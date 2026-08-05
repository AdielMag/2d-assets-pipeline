import sqlite3
import sys

def run():
    db_path = r"c:\Users\Adiel\2D Assets Pipline\storage\app.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    cur.execute("SELECT id, asset_id, provider, composed_prompt, raw_path, processed_path, model FROM asset_versions WHERE asset_id IN (38, 39)")
    versions = cur.fetchall()
    
    print("VERSIONS for Assets 38 and 39:")
    for v in versions:
        print(f"  {v}")
        
if __name__ == "__main__":
    run()
