import pyodbc
import time
import sys

def run_test(use_packet_size_1024):
    print("=" * 60)
    print(f"RUNNING TEST: use_packet_size_1024 = {use_packet_size_1024}")
    print("=" * 60)
    
    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        "SERVER=10.101.1.21;"
        "UID=frapa;"
        "PWD=Pirtek@4098;"
        "Encrypt=Optional;TrustServerCertificate=yes;"
        "Connection Timeout=30;"
        "DATABASE=US_PUSA_App;"
        "KeepAlive=30;"
        "KeepAliveInterval=1;"
    )
    if use_packet_size_1024:
        conn_str += "PacketSize=1024;"
        
    print("Connecting to SQL Server...")
    start_time = time.time()
    try:
        conn = pyodbc.connect(conn_str, autocommit=True)
        # Configure UTF-16LE decoding for NVARCHAR / WCHAR columns and metadata on Linux
        conn.setdecoding(pyodbc.SQL_CHAR, encoding='utf-8')
        conn.setdecoding(pyodbc.SQL_WCHAR, encoding='utf-16le')
        conn.setdecoding(pyodbc.SQL_WMETADATA, encoding='utf-16le')
        conn.setencoding(encoding='utf-8')
        print(f"Connected successfully in {time.time() - start_time:.2f}s.")
    except Exception as e:
        print(f"Connection FAILED: {e}")
        return

    try:
        cursor = conn.cursor()
        print("Setting session parameters...")
        cursor.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        cursor.execute("SET NOCOUNT ON")
        cursor.execute("SET ARITHABORT ON")
        cursor.execute("SET ANSI_WARNINGS ON")
        cursor.execute("SET ANSI_NULLS ON")
        cursor.execute("SET QUOTED_IDENTIFIER ON")
        cursor.execute("SET CONCAT_NULL_YIELDS_NULL ON")
        cursor.execute("SET ANSI_PADDING ON")
        cursor.execute("SET NUMERIC_ROUNDABORT OFF")
        print("Session parameters set.")

        query = "EXEC PTK_IT_GetAccountsPayableAging '2026-06-30 00:00:00' WITH RECOMPILE"
        print(f"Executing query: {query}")
        
        exec_start = time.time()
        cursor.execute(query)
        print(f"cursor.execute() completed in {time.time() - exec_start:.2f}s.")
        
        set_idx = 1
        while True:
            print(f"Checking Result Set #{set_idx} description...")
            desc_start = time.time()
            desc = cursor.description
            print(f"cursor.description retrieved in {time.time() - desc_start:.6f}s. Has data: {desc is not None}")
            
            if desc:
                columns = [col[0] for col in desc]
                print(f"Columns: {columns}")
                print(f"Fetching all rows for Result Set #{set_idx}...")
                fetch_start = time.time()
                rows = cursor.fetchall()
                print(f"Fetched {len(rows)} rows in {time.time() - fetch_start:.2f}s.")
                
            print(f"Calling cursor.nextset() for Result Set #{set_idx}...")
            nextset_start = time.time()
            has_more = cursor.nextset()
            print(f"cursor.nextset() returned {has_more} in {time.time() - nextset_start:.2f}s.")
            
            if not has_more:
                print("No more result sets. Loop ending.")
                break
            set_idx += 1
            
        print(f"Test completed successfully in {time.time() - start_time:.2f}s.")
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"ERROR during test: {e}")
        try:
            conn.close()
        except:
            pass

if __name__ == "__main__":
    mode = "both"
    if len(sys.argv) > 1:
        mode = sys.argv[1]
        
    if mode in ("1024", "both"):
        run_test(use_packet_size_1024=True)
    if mode in ("default", "both"):
        run_test(use_packet_size_1024=False)
