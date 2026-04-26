import paramiko
import time

def test_ssh(ip, port, username, password):
    print(f"Testing SSH connection to {username}@{ip}:{port} ...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=ip,
            port=port,
            username=username,
            password=password,
            timeout=5
        )
        print(f"[SUCCESS] Connected to {ip}")
        stdin, stdout, stderr = client.exec_command('uname -a')
        print(f"System Info: {stdout.read().decode().strip()}")
        client.close()
        return True
    except Exception as e:
        print(f"[FAILED] Could not connect to {ip}: {e}")
        return False

if __name__ == "__main__":
    # Test on the connected devices found via adb
    ips = ["192.168.16.132", "192.168.16.133"]
    port = 8080
    username = "thunder"
    password = "Thunder#123"
    
    for ip in ips:
        test_ssh(ip, port, username, password)
