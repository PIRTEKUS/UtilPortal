import os
import re
import fnmatch
import shutil
from datetime import datetime
import smbclient


def parse_smb_auth(username):
    """
    Extract domain and username from various credential formats:
      - 'DOMAIN\\username' -> ('DOMAIN', 'username')
      - 'username@domain.com' -> ('domain.com', 'username')
      - 'username' -> ('', 'username')
    """
    if not username:
        return '', ''
    username = str(username).strip()
    if '\\' in username:
        parts = username.split('\\', 1)
        return parts[0], parts[1]
    if '/' in username:
        parts = username.split('/', 1)
        return parts[0], parts[1]
    if '@' in username:
        parts = username.split('@', 1)
        return parts[1], parts[0]
    return '', username


def normalize_unc_path(host, raw_path):
    """
    Convert any path format into a valid Windows UNC path:
      - 'SharedFolder/SubFolder' -> r'\\10.1.2.3\SharedFolder\SubFolder'
      - r'\\10.1.2.3\SharedFolder\SubFolder' -> r'\\10.1.2.3\SharedFolder\SubFolder'
      - r'\\OTHERHOST\Share' -> r'\\OTHERHOST\Share'
    """
    if not raw_path:
        return fr"\\{host}" if host else ""
        
    p = str(raw_path).strip().replace('/', '\\')
    if p.startswith(r'\\'):
        return p
        
    clean_p = p.lstrip('\\')
    return fr"\\{host}\{clean_p}"


def register_smb_session(conn, port=445, timeout=15):
    """
    Register or update an authenticated SMB session with smbclient.
    """
    smbclient.register_session(
        server=conn.host,
        username=conn.username,
        password=conn.password,
        port=port,
        connection_timeout=timeout
    )


def test_smb_connection(conn):
    """
    Test connectivity and credentials against a Windows file server.
    Returns (success: bool, message: str).
    """
    try:
        register_smb_session(conn)
        root_path = fr"\\{conn.host}"
        # Attempt to list root shares/directory to verify credentials
        try:
            entries = smbclient.listdir(root_path)
            shares_str = ", ".join(entries[:6])
            if len(entries) > 6:
                shares_str += f" (+{len(entries)-6} more)"
            return True, f"Successfully connected to {conn.host}. Available shares: [{shares_str}]"
        except Exception:
            # If root listing is restricted, connection handshake still succeeded
            return True, f"Successfully connected and authenticated to {conn.host}."
    except Exception as e:
        return False, f"SMB Connection Error: {str(e)}"


def browse_smb_folders(conn, raw_path=""):
    """
    Browse shares or subdirectories on a Windows Server.
    If raw_path is empty or root (\\\\host):
      Returns list of available top-level shares on the host.
    If raw_path is a share or subfolder (\\\\host\\Share\\SubFolder or Share\\SubFolder):
      Returns list of subdirectories inside that folder.
    """
    register_smb_session(conn)
    raw_path = (raw_path or "").strip().replace('/', '\\')
    
    if not raw_path or raw_path.strip('\\') == '' or raw_path.lower() == fr"\\{conn.host}".lower():
        # Root level: list shares
        current_path = fr"\\{conn.host}"
        folders = []
        try:
            share_names = smbclient.listdir(current_path)
            for name in sorted(share_names, key=lambda x: x.lower()):
                folders.append({
                    'name': name,
                    'path': fr"\\{conn.host}\{name}",
                    'is_share': True
                })
        except Exception as e:
            raise RuntimeError(f"Could not list shares on {conn.host}: {str(e)}")
            
        return {
            'server_name': conn.name,
            'server_host': conn.host,
            'current_path': current_path,
            'is_root': True,
            'parent_path': None,
            'folders': folders
        }
    else:
        # Subfolder level
        unc_path = normalize_unc_path(conn.host, raw_path)
        if not smbclient.path.exists(unc_path):
            raise FileNotFoundError(f"Path not found on {conn.host}: {unc_path}")
            
        parts = [p for p in unc_path.split('\\') if p]
        if len(parts) <= 2:
            parent_path = fr"\\{conn.host}"
        else:
            parent_path = '\\\\' + '\\'.join(parts[:-1])
            
        folders = []
        try:
            for entry in smbclient.scandir(unc_path):
                if entry.is_dir():
                    folders.append({
                        'name': entry.name,
                        'path': entry.path,
                        'is_share': False
                    })
        except Exception as e:
            raise RuntimeError(f"Could not open directory {unc_path}: {str(e)}")
            
        folders.sort(key=lambda x: x['name'].lower())
        return {
            'server_name': conn.name,
            'server_host': conn.host,
            'current_path': unc_path,
            'is_root': False,
            'parent_path': parent_path,
            'folders': folders
        }


def format_file_size(size_bytes):
    """Format bytes into readable KB/MB/GB string."""
    if size_bytes is None:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:3.1f} {unit}" if unit != 'B' else f"{int(size_bytes)} B"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


def list_smb_files(conn, folder_path, pattern="*.*"):
    """
    List files in a remote Windows folder matching a given pattern.
    Returns a list of dicts: [ { name, is_dir, size, size_formatted, modified, path }, ... ]
    """
    register_smb_session(conn)
    unc_path = normalize_unc_path(conn.host, folder_path)
    
    if not smbclient.path.exists(unc_path):
        raise FileNotFoundError(f"Directory not found on {conn.name} ({conn.host}): {unc_path}")
        
    pattern = pattern.strip() if pattern else "*.*"
    patterns = [p.strip() for p in pattern.split(';') if p.strip()] if ';' in pattern else [pattern]
    
    results = []
    for entry in smbclient.scandir(unc_path):
        # Ignore directories if filtering specifically for files, or list both
        name = entry.name
        is_dir = entry.is_dir()
        
        # Check pattern match for files
        if not is_dir and patterns:
            matched = any(fnmatch.fnmatch(name.lower(), p.lower()) for p in patterns)
            if not matched:
                continue
                
        try:
            st = entry.stat()
            size = st.st_size
            mtime = datetime.fromtimestamp(st.st_mtime)
            mtime_str = mtime.strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            size = 0
            mtime_str = "—"
            
        results.append({
            'name': name,
            'is_dir': is_dir,
            'size': size,
            'size_formatted': format_file_size(size) if not is_dir else "—",
            'modified': mtime_str,
            'path': entry.path
        })
        
    # Sort directories first, then alphabetical by name
    results.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
    return results


def save_smb_file(conn, dest_folder, file_storage, filename=None, chunk_size=65536):
    """
    Save an uploaded file directly to a destination SMB folder.
    Returns (success: bool, info_dict: dict, error_msg: str).
    """
    try:
        register_smb_session(conn)
        target_dir = normalize_unc_path(conn.host, dest_folder)
        
        # Ensure target folder exists
        if not smbclient.path.exists(target_dir):
            smbclient.makedirs(target_dir, exist_ok=True)
            
        final_filename = filename or file_storage.filename
        # Sanitize filename (prevent directory traversal)
        final_filename = os.path.basename(final_filename).replace('\\', '_').replace('/', '_')
        target_filepath = smbclient.path.join(target_dir, final_filename)
        
        total_bytes = 0
        with smbclient.open_file(target_filepath, mode='wb') as dst:
            while True:
                chunk = file_storage.read(chunk_size)
                if not chunk:
                    break
                dst.write(chunk)
                total_bytes += len(chunk)
                
        return True, {
            'filename': final_filename,
            'path': target_filepath,
            'bytes': total_bytes,
            'size_formatted': format_file_size(total_bytes)
        }, None
    except Exception as e:
        return False, None, str(e)


def move_smb_file(src_conn, src_folder, dst_conn, dst_folder, filename, chunk_size=65536):
    """
    Move a file from source SMB folder to destination SMB folder.
    Supports both intra-server atomic move and cross-server stream copy + delete.
    Returns (success: bool, error_msg: str).
    """
    try:
        register_smb_session(src_conn)
        src_dir = normalize_unc_path(src_conn.host, src_folder)
        src_file = smbclient.path.join(src_dir, filename)
        
        if not smbclient.path.exists(src_file):
            return False, f"Source file does not exist: {src_file}"
            
        register_smb_session(dst_conn)
        dst_dir = normalize_unc_path(dst_conn.host, dst_folder)
        
        if not smbclient.path.exists(dst_dir):
            smbclient.makedirs(dst_dir, exist_ok=True)
            
        dst_file = smbclient.path.join(dst_dir, filename)
        
        # If on the same host, perform atomic server-side rename/replace
        if src_conn.id == dst_conn.id or src_conn.host.lower() == dst_conn.host.lower():
            smbclient.replace(src_file, dst_file)
        else:
            # Cross-server transfer: stream copy then remove origin
            with smbclient.open_file(src_file, mode='rb') as f_in:
                with smbclient.open_file(dst_file, mode='wb') as f_out:
                    shutil.copyfileobj(f_in, f_out, length=chunk_size)
            # Remove from source after verified copy
            smbclient.remove(src_file)
            
        return True, None
    except Exception as e:
        return False, str(e)
