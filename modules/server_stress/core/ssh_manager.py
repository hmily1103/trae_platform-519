import json
import os
import threading
import logging
import traceback
import base64
import hashlib

logger = logging.getLogger(__name__)

try:
    import paramiko
except ImportError:
    paramiko = None
    logger.warning("Paramiko not found. SSH features will be disabled.")

# 密码加密：使用 Fernet，密钥来自环境变量 STRESS_ENCRYPT_KEY 或派生自默认盐
_fernet = None

def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    try:
        from cryptography.fernet import Fernet
        key = os.environ.get('STRESS_ENCRYPT_KEY')
        if key:
            _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        else:
            salt = os.environ.get('FLASK_SECRET_KEY', 'trae_stress_default_salt')
            key = base64.urlsafe_b64encode(hashlib.sha256(salt.encode()).digest())
            _fernet = Fernet(key)
        return _fernet
    except Exception as e:
        logger.warning(f"Fernet init failed, passwords stored in plaintext: {e}")
        return None

def _encrypt_password(pwd):
    if not pwd:
        return pwd
    f = _get_fernet()
    if not f:
        return pwd
    try:
        return '_enc:' + f.encrypt(pwd.encode()).decode()
    except Exception:
        return pwd

def _decrypt_password(pwd):
    if not pwd or not isinstance(pwd, str) or not pwd.startswith('_enc:'):
        return pwd
    f = _get_fernet()
    if not f:
        return pwd
    try:
        return f.decrypt(pwd[5:].encode()).decode()
    except Exception:
        return pwd


def _format_ssh_error(exc, host, port):
    """将 Paramiko/SSH 异常转为用户可读的提示"""
    err_str = str(exc).lower()
    port = int(port) if port else 222

    if 'error reading ssh protocol banner' in err_str or 'eof' in err_str:
        hint = ""
        if port == 8080:
            hint = "【重要】8080 通常是 Web 端口，不是 SSH 端口。若您是通过 192.168.x.x:8080 访问本平台，请将 SSH 端口改为 222。"
        return (
            f"无法建立 SSH 连接（{host}:{port}）。"
            f"{hint}"
            "可能原因：① 端口错误（SSH 默认 222，非 8080）；② 该端口为 HTTP 而非 SSH；③ 防火墙/网络阻断；④ SSH 服务未启动。"
        )
    if 'connection refused' in err_str:
        return f"连接被拒绝（{host}:{port}）。请检查：① 端口是否正确；② SSH 服务是否已启动。"
    if 'timed out' in err_str or 'timeout' in err_str:
        return f"连接超时（{host}:{port}）。请检查：① 网络是否可达；② 防火墙是否放行该端口。"
    if 'authentication failed' in err_str or 'no valid authentication' in err_str:
        return "认证失败。请检查用户名和密码是否正确。"
    if 'no hostkey for host' in err_str or 'host key verification' in err_str:
        return "主机密钥验证失败。请确认目标主机地址正确。"
    if 'connection reset' in err_str:
        return f"连接被重置（{host}:{port}）。可能被防火墙或安全策略中断。"
    if 'network is unreachable' in err_str:
        return f"网络不可达（{host}）。请检查 IP 地址和网络连接。"

    return f"SSH 连接失败: {exc}"


class SSHManager:
    _instance = None
    _lock = threading.Lock()
    
    def __init__(self, data_dir):
        self.data_dir = data_dir
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)
        self.servers_file = os.path.join(self.data_dir, 'servers.json')
        self.servers = self._load_servers()
        self.active_connections = {} # server_id -> client

    @classmethod
    def get_instance(cls, app_root=None):
        if not cls._instance and app_root:
            with cls._lock:
                if not cls._instance:
                    data_dir = os.path.join(app_root, 'logs', 'server_stress_data')
                    cls._instance = SSHManager(data_dir)
        return cls._instance

    def _load_servers(self):
        if not os.path.exists(self.servers_file):
            return []
        try:
            with open(self.servers_file, 'r', encoding='utf-8') as f:
                servers = json.load(f)
            # 迁移：明文密码加密后重写
            changed = False
            for s in servers:
                pwd = s.get('password')
                if pwd and pwd != '***' and not str(pwd).startswith('_enc:'):
                    s['password'] = _encrypt_password(pwd)
                    changed = True
            if changed:
                with open(self.servers_file, 'w', encoding='utf-8') as f:
                    json.dump(servers, f, indent=2, ensure_ascii=False)
            return servers
        except Exception as e:
            logger.error(f"Failed to load servers: {e}")
            return []

    def _save_servers(self):
        try:
            with open(self.servers_file, 'w', encoding='utf-8') as f:
                json.dump(self.servers, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save servers: {e}")

    def add_server(self, server_info):
        # server_info: {id, name, ip, port, username, password, ...}
        info = dict(server_info)
        if info.get('password'):
            info['password'] = _encrypt_password(info['password'])
        self.servers.append(info)
        self._save_servers()

    def remove_server(self, server_id):
        self.servers = [s for s in self.servers if s.get('id') != server_id]
        self._save_servers()

    def update_server(self, server_id, updates):
        """更新服务器配置（支持修改端口等）"""
        for s in self.servers:
            if s.get('id') == server_id:
                if self.is_simulated_slave(s):
                    return False
                for k, v in updates.items():
                    if k == 'password':
                        if v and v != '***':
                            s['password'] = _encrypt_password(v)
                    elif k in ('name', 'ip', 'port', 'username'):
                        if k == 'port':
                            s[k] = int(v) if v is not None and v != '' else 222
                        else:
                            s[k] = v
                self._save_servers()
                return True
        return False

    def get_servers(self):
        """返回服务器列表，密码脱敏（不返回给前端）"""
        return [{**s, 'password': '***' if s.get('password') else ''} for s in self.servers]

    def get_server_for_use(self, server_id):
        """内部使用：按 id 获取服务器，密码已解密"""
        for s in self.servers:
            if s.get('id') == server_id:
                out = dict(s)
                if out.get('password') and out['password'] != '***':
                    out['password'] = _decrypt_password(out['password'])
                return out
        return None

    @staticmethod
    def is_simulated_slave(server):
        """是否为「模拟从机」：不连真实 SSH，用于一主一从场景模拟（从机高负载/死机）"""
        return server and (server.get('simulated') is True or server.get('ip') == 'sim://slave')

    def test_connection(self, server_info):
        if self.is_simulated_slave(server_info):
            return True, "模拟从机，无需真实连接。可用于模拟一主一从、从机高负载场景。", None
        if not paramiko:
            return False, "Paramiko library is missing. Please install it in the backend environment.", None
        
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=server_info['ip'],
                port=int(server_info.get('port', 222)),
                username=server_info['username'],
                password=server_info.get('password'),
                timeout=5
            )
            client.close()
            return True, "Connection successful.", None
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"SSH Connection Error: {e}\n{tb}")
            host = server_info.get('ip', '')
            port = server_info.get('port', 222)
            msg = _format_ssh_error(e, host, port)
            return False, msg, tb

    def exec_command(self, server, cmd, timeout=10):
        if self.is_simulated_slave(server):
            return self._exec_simulated_slave(server, cmd)
        # Mock Server Logic (generic mock)
        if server.get('ip') == 'mock':
            return self._exec_mock(server, cmd)

        if not paramiko:
            return False, "Paramiko not available"
            
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=server['ip'],
                port=int(server.get('port', 222)),
                username=server['username'],
                password=server.get('password'),
                timeout=5
            )
            stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
            out = stdout.read().decode('utf-8')
            err = stderr.read().decode('utf-8')
            client.close()
            
            if err and not out:
                # Sometimes warnings go to stderr but command succeeded
                # Special handling for nohup/background commands?
                # For now return False if stderr has content unless it's just a warning
                pass
            
            return True, out + err
        except Exception as e:
            host = server.get('ip', '')
            port = server.get('port', 222)
            msg = _format_ssh_error(e, host, port)
            return False, msg

    def _exec_mock(self, server, cmd):
        import random
        import re
        
        # Init mock state if needed
        if not hasattr(self, '_mock_states'):
            self._mock_states = {}
            
        server_id = server.get('id')
        
        # 1. Check tool
        if "which stress-ng" in cmd:
            return True, "/usr/bin/stress-ng\n"
        
        # 2. Install tool
        if "apt-get install" in cmd:
            return True, "Mock installation successful"

        # 3. Start Stress
        if "stress-ng" in cmd and "nohup" in cmd:
            # Parse load
            match = re.search(r'--cpu-load (\d+)', cmd)
            load = int(match.group(1)) if match else 50
            self._mock_states[server_id] = load
            return True, str(random.randint(1000, 9999))
            
        # 4. Stop Stress
        if "kill" in cmd:
            self._mock_states[server_id] = 5 # Reset to idle
            return True, ""
            
        # 5. Stats (vmstat/free)
        if "vmstat" in cmd:
            current_load = self._mock_states.get(server_id, 5) # Default 5%
            
            # Fluctuate slightly
            actual_load = current_load + random.randint(-5, 5)
            actual_load = max(1, min(100, actual_load))
            idle = 100 - actual_load
            
            # free -m output
            # Mem: total used free ...
            mem_total = 8192
            mem_used = 4096 + random.randint(-500, 500)
            if actual_load > 80: # Simulate high memory usage too
                mem_used += 2000
                
            mem_line = f"Mem: {mem_total} {mem_used} {mem_total-mem_used} 0 0 0"
            
            # vmstat output
            # r b swpd free buff cache si so bi bo in cs us sy id wa st
            # We put idle in the 3rd from last column (id)
            # 14 columns before us
            prefix = "0 0 0 0 0 0 0 0 0 0 0 0"
            vm_line = f"{prefix} {100-idle} 0 {idle} 0 0"
            
            return True, f"{mem_line}\n{vm_line}"
            
        return True, "Mock output"

    def _exec_simulated_slave(self, server, cmd):
        """
        模拟从机：不执行真实命令，仅返回「从机高负载」的监控数据，
        用于在只有一台物理机时模拟一主一从、从机 CPU 过高的测试场景。
        """
        import random
        server_id = server.get('id')
        if not hasattr(self, '_sim_slave_states'):
            self._sim_slave_states = {}
        state = self._sim_slave_states.setdefault(server_id, {'cpu': 92, 'mem': 78})

        # 检查/安装 stress-ng：模拟已就绪
        if 'which stress-ng' in cmd:
            return True, "/usr/bin/stress-ng\n"
        if 'apt-get install' in cmd:
            return True, "Simulated slave: skip real install.\n"

        # 开始压测：仅记录状态，不真跑（避免真机负载）
        if 'stress-ng' in cmd and 'nohup' in cmd:
            import re
            match = re.search(r'--cpu-load (\d+)', cmd)
            state['cpu'] = min(99, int(match.group(1)) + 5) if match else 95
            state['mem'] = min(95, state.get('mem', 78) + 10)
            return True, "9998"
        if 'kill' in cmd:
            state['cpu'] = 88
            state['mem'] = 75
            return True, ""

        # 监控数据：模拟从机高 CPU/高内存（带小幅波动）
        if 'vmstat' in cmd or 'free -m' in cmd:
            cpu = state['cpu'] + random.randint(-3, 3)
            cpu = max(80, min(99, cpu))
            mem = state['mem'] + random.randint(-2, 2)
            mem = max(70, min(95, mem))
            mem_total = 8192
            mem_used = int(mem_total * mem / 100)
            mem_line = f"Mem: {mem_total} {mem_used} {mem_total - mem_used} 0 0 0"
            idle = 100 - cpu
            vm_line = "0 0 0 0 0 0 0 0 0 0 0 0 " + f"{100 - idle} 0 {idle} 0 0"
            return True, f"{mem_line}\n{vm_line}"

        return True, "simulated slave ok"
