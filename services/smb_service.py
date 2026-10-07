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


def join_unc_path(base_dir, filename):
    """
    Safely join a UNC folder path and a filename or subfolder,
    ensuring standard Windows backslashes and avoiding double slashes.
    """
    clean_base = str(base_dir or '').replace('/', '\\').rstrip('\\')
    clean_file = str(filename or '').replace('/', '\\').lstrip('\\')
    if not clean_base:
        return clean_file
    if not clean_file:
        return clean_base
    return f"{clean_base}\\{clean_file}"


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
        session = register_smb_session(conn, timeout=10)
        # Probe common shares to verify tree connection capability
        candidates = ['C$', 'D$', 'E$', 'Shared', 'Share', 'Shares', 'Data', 'Users', 'Public', 'Uploads', 'Files']
        from smbprotocol.tree import TreeConnect
        found_shares = []
        for s in candidates:
            try:
                tree = TreeConnect(session, fr"\\{conn.host}\{s}")
                tree.connect()
                found_shares.append(s)
            except Exception:
                pass
        
        if found_shares:
            shares_str = ", ".join(found_shares[:6])
            return True, f"Successfully connected and authenticated to {conn.host}. Available shares: [{shares_str}]"
        return True, f"Successfully connected and authenticated to {conn.host}."
    except Exception as e:
        err = str(e)
        if 'timed out' in err.lower():
            return False, f"SMB Connection Error: Failed to connect to '{conn.host}:445' (timed out). Please verify that port 445 is reachable from this server and that Windows Firewall allows inbound SMB connections."
        return False, f"SMB Connection Error: {err}"


def browse_smb_folders(conn, raw_path=""):
    """
    Browse shares or subdirectories on a Windows Server.
    If raw_path is empty or root (\\\\host):
      Returns list of available top-level shares on the host.
    If raw_path is a share or subfolder (\\\\host\\Share\\SubFolder or Share\\SubFolder):
      Returns list of subdirectories inside that folder.
    """
    session = register_smb_session(conn, timeout=10)
    raw_path = (raw_path or "").strip().replace('/', '\\')
    
    clean_parts = [p for p in raw_path.split('\\') if p]
    
    # Check if we are at the root level (no share specified)
    is_root = False
    if not clean_parts:
        is_root = True
    elif len(clean_parts) == 1 and clean_parts[0].lower() == conn.host.lower():
        is_root = True

    if is_root:
        current_path = fr"\\{conn.host}"
        folders = []
        
        candidates = [
            'C$', 'D$', 'E$', 'Shared', 'Share', 'Shares', 'Data', 'Users', 
            'Public', 'Uploads', 'Incoming', 'Archive', 'Files', 'Backups', 
            'IT', 'Logs', 'Apps', 'Transfer', 'Temp', 'Software', 'Reports',
            'Documents', 'Media', 'Storage', 'Export', 'Import'
        ]
        from smbprotocol.tree import TreeConnect
        
        for share_name in candidates:
            try:
                tree = TreeConnect(session, fr"\\{conn.host}\{share_name}")
                tree.connect()
                folders.append({
                    'name': share_name,
                    'path': fr"\\{conn.host}\{share_name}",
                    'is_share': True
                })
            except Exception:
                pass
                
        folders.sort(key=lambda x: (not x['name'].endswith('$'), x['name'].lower()))
        
        return {
            'server_name': conn.name,
            'server_host': conn.host,
            'current_path': current_path,
            'is_root': True,
            'parent_path': None,
            'folders': folders
        }
    else:
        # Subfolder or specific Share level
        unc_path = normalize_unc_path(conn.host, raw_path)
        parts = [p for p in unc_path.split('\\') if p]
        
        share_name = parts[1] if len(parts) > 1 else ""
        
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
            err_msg = str(e)
            if '0xc00000cc' in err_msg or 'STATUS_BAD_NETWORK_NAME' in err_msg:
                raise RuntimeError(f"Share '{share_name}' was not found on server {conn.host}. Please verify the Windows share name or try typing the exact share name.")
            elif '0xc0000022' in err_msg or 'STATUS_ACCESS_DENIED' in err_msg:
                raise RuntimeError(f"Access denied to '{unc_path}'. User '{conn.username}' does not have permission to access this share/folder.")
            else:
                raise RuntimeError(f"Could not open directory {unc_path}: {err_msg}")
                
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
        target_filepath = join_unc_path(target_dir, final_filename)
        
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
        src_file = join_unc_path(src_dir, filename)
        
        if not smbclient.path.exists(src_file):
            return False, f"Source file does not exist: {src_file}"
            
        register_smb_session(dst_conn)
        dst_dir = normalize_unc_path(dst_conn.host, dst_folder)
        
        if not smbclient.path.exists(dst_dir):
            smbclient.makedirs(dst_dir, exist_ok=True)
            
        dst_file = join_unc_path(dst_dir, filename)
        
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
