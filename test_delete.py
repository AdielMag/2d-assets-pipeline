import urllib.request, json, time

base = 'http://127.0.0.1:8787/api'

# Create an atlas
req = urllib.request.Request(f'{base}/projects/1/atlases', data=b'{"name": "TestDomain", "parent_id": null}', headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req) as response:
    atlas = json.loads(response.read().decode())
    atlas_id = atlas['id']
    print('Created atlas:', atlas_id)

# Create a child atlas (subdomain)
req = urllib.request.Request(f'{base}/projects/1/atlases', data=f'{{"name": "ChildDomain", "parent_id": {atlas_id}}}'.encode(), headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(req) as response:
    child_atlas = json.loads(response.read().decode())
    print('Created child atlas:', child_atlas['id'])

# Check atlases before
req = urllib.request.Request(f'{base}/projects/1/atlases')
with urllib.request.urlopen(req) as response:
    atlases = json.loads(response.read().decode())
    print('Atlases before delete:', [a['id'] for a in atlases])

# Delete with content_only
req = urllib.request.Request(f'{base}/atlases/{atlas_id}?mode=content_only', method='DELETE')
with urllib.request.urlopen(req) as response:
    print('Delete response:', response.read().decode())

# Check if parent atlas still exists
req = urllib.request.Request(f'{base}/projects/1/atlases')
with urllib.request.urlopen(req) as response:
    atlases = json.loads(response.read().decode())
    print('Atlases after delete:', [a['id'] for a in atlases])
