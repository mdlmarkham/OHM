import subprocess, json, os

restricted = {'PATH': os.environ.get('PATH', ''), 'OHM_TEST': '1'}

proc = subprocess.Popen(
    'python -c "import os,json; print(json.dumps(sorted(os.environ.keys())))"',
    shell=True,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env=restricted,
)
out, err = proc.communicate()
print("=== Shell=True ===")
print(out.decode(errors="replace"))
if err.decode(errors="replace").strip():
    print("stderr:", err.decode(errors="replace")[:200])

proc2 = subprocess.Popen(
    ['python', '-c', 'import os,json; print(json.dumps(sorted(os.environ.keys())))'],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env=restricted,
)
out2, err2 = proc2.communicate()
print("\n=== Shell=False ===")
print(out2.decode(errors="replace"))
