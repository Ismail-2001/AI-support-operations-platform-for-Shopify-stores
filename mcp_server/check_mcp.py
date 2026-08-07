import subprocess

print("MCP version check:")
result = subprocess.run(["pip", "--version"], capture_output=True, text=True)
print(result.stdout)
