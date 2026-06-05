import subprocess, sys
result = subprocess.run(
    [sys.executable, 'verify_l5.py'],
    capture_output=True,
    text=True,
    timeout=60
)
output = result.stdout + result.stderr
print(f"==== RETURN CODE: {result.returncode} ====")
print("==== STDOUT ====")
for line in result.stdout.split('\n'):
    clean = line.split('\r')[-1]
    print(clean)
print("==== STDERR ====")
for line in result.stderr.split('\n'):
    clean = line.split('\r')[-1]
    print(clean)
