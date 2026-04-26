import threading
import time
import logging
from .ssh_manager import SSHManager
from core.runtime import get_runtime_manager, RuntimeStatus

logger = logging.getLogger(__name__)

class StressManager:
    _instance = None
    _lock = threading.Lock()
    
    def __init__(self):
        self.active_stress_jobs = {} # server_id -> { 'status': 'running', 'pid': ..., 'command': ..., 'runtime_id': ... }
        self._jobs_lock = threading.Lock()  # 实例级锁，保护 active_stress_jobs 的并发访问
        self.ssh_manager = None # Will be injected or retrieved
        
        # Start background monitor for job completion
        self._monitor_thread = threading.Thread(target=self._monitor_jobs, daemon=True)
        self._monitor_thread.start()

    def _monitor_jobs(self):
        """Background thread to check for expired stress jobs"""
        while True:
            time.sleep(5)
            try:
                now = time.time()
                # 在锁内复制字典，避免迭代时并发修改
                with self._jobs_lock:
                    current_jobs = list(self.active_stress_jobs.items())
                
                for server_id, job in current_jobs:
                    if job['status'] == 'running':
                        end_time = job['start_time'] + job['duration']
                        if now > end_time:
                            # Job finished naturally
                            self._complete_job(server_id, job, RuntimeStatus.COMPLETED)
                            
            except Exception as e:
                logger.error(f"Error in stress monitor: {e}")

    def _complete_job(self, server_id, job, status):
        """Mark job as complete in RuntimeManager and remove from local tracking"""
        runtime_id = job.get('runtime_id')
        if runtime_id:
            try:
                get_runtime_manager().update_status(runtime_id, status)
            except Exception as e:
                logger.error(f"Failed to update runtime status: {e}")
        
        with self._jobs_lock:
            if server_id in self.active_stress_jobs:
                del self.active_stress_jobs[server_id]

    @classmethod
    def get_instance(cls):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = StressManager()
        return cls._instance

    def set_ssh_manager(self, manager):
        self.ssh_manager = manager

    def check_tool_installed(self, server_id):
        """Check if stress-ng is installed on the target server"""
        if not self.ssh_manager:
            return False, "SSH Manager not initialized"
        
        server = self._get_server_by_id(server_id)
        if not server:
            return False, "Server not found"

        # Check if stress-ng is installed
        # Skip check for simulated servers to allow simulation logic to proceed (which handles its own mocking)
        if not (server.get('simulated') is True or server.get('ip') == 'sim://slave' or server.get('ip') == 'mock'):
             installed, msg = self.check_tool_installed(server_id)
             if not installed:
                 return False, f"未检测到 stress-ng 工具。请先点击'安装 stress-ng 工具'按钮。"

            
        success, output = self._exec_ssh_command(server, "which stress-ng")
        if success and "/stress-ng" in output:
            return True, "Installed"
        return False, "Not installed"

    def install_tool(self, server_id):
        """安装 stress-ng，支持 apt-get (Debian/Ubuntu) 和 yum/dnf (CentOS/RHEL)"""
        server = self._get_server_by_id(server_id)
        if not server:
            return False, "服务器不存在"

        # 1. Check if already installed
        ok, out = self._exec_ssh_command(server, "which stress-ng")
        if ok and out and "/stress-ng" in out:
            return True, "stress-ng 已安装，无需重复安装。"

        pwd = server.get('password', '') or ''
        is_root = (server.get('username') == 'root')
        
        # Helper to construct sudo command
        def sudo_cmd(c):
            return c if is_root else f"echo '{pwd}' | sudo -S {c}"

        # 2. Check Package Manager
        # Try apt-get
        ok, _ = self._exec_ssh_command(server, "which apt-get")
        if ok:
            cmd = f"{sudo_cmd('apt-get update')} && {sudo_cmd('apt-get install -y stress-ng')}"
            success, output = self._exec_ssh_command(server, cmd, timeout=600)
            if success: return True, "安装成功 (apt-get)"
            return False, f"apt-get 安装失败: {output}"

        # Try yum/dnf
        for pkg_mgr in ['dnf', 'yum']:
            ok, _ = self._exec_ssh_command(server, f"which {pkg_mgr}")
            if ok:
                # CentOS usually needs EPEL for stress-ng
                install_epel = f"{sudo_cmd(f'{pkg_mgr} install -y epel-release')}"
                install_tool = f"{sudo_cmd(f'{pkg_mgr} install -y stress-ng')}"
                cmd = f"{install_epel} && {install_tool}"
                success, output = self._exec_ssh_command(server, cmd, timeout=600)
                if success: return True, f"安装成功 ({pkg_mgr})"
                return False, f"{pkg_mgr} 安装失败: {output}"

        return False, "未找到支持的包管理器 (apt-get/yum/dnf)"

    def start_stress(self, server_id, cpu_cores=0, cpu_load=100, timeout=60, vm_workers=0, vm_bytes="256M", io_workers=0):
        """
        Start stress-ng on the server.
        cpu_cores: 0 means all cores.
        cpu_load: percentage 0-100.
        timeout: seconds.
        vm_workers: number of vm workers (memory stress).
        vm_bytes: size of memory per worker (e.g. 256M, 1G).
        io_workers: number of io workers (disk I/O stress).
        """
        server = self._get_server_by_id(server_id)
        if not server:
            return False, "Server not found"

        # Check if stress-ng is installed
        # Skip check for simulated servers to allow simulation logic to proceed
        if not (server.get('simulated') is True or server.get('ip') == 'sim://slave' or server.get('ip') == 'mock'):
             installed, msg = self.check_tool_installed(server_id)
             if not installed:
                 return False, f"未检测到 stress-ng 工具。请先点击'安装 stress-ng 工具'按钮。"

        # Construct command
        # --timeout T : stop after T seconds
        # & : run in background
        cmd_parts = [f"nohup stress-ng --timeout {timeout}s"]
        
        # CPU Stress
        if cpu_cores > 0 or (vm_workers == 0 and io_workers == 0): # Default to CPU if nothing selected
            c = cpu_cores if cpu_cores > 0 else 0 # 0 usually means 1 per core in stress-ng? No, 0 in stress-ng means "one per online CPU"
            # stress-ng --cpu 0 means all online CPUs
            cmd_parts.append(f"--cpu {c} --cpu-load {cpu_load}")
            
        # Memory Stress
        if vm_workers > 0:
            cmd_parts.append(f"--vm {vm_workers} --vm-bytes {vm_bytes}")
            
        # I/O Stress
        if io_workers > 0:
            cmd_parts.append(f"--io {io_workers}")

        cmd_parts.append("> /dev/null 2>&1 & echo $!")
        inner_cmd = " ".join(cmd_parts)
        # Wrap in sh -c to ensure correct shell handling of background operator and variable expansion
        cmd = f"sh -c '{inner_cmd}'"
        
        logger.info(f"Starting stress on server {server_id} with cmd: {cmd}")
        success, output = self._exec_ssh_command(server, cmd)
        logger.info(f"Start stress output: {repr(output)}")

        if success:
            # Try to find PID using regex to be more robust against noise
            import re
            
            output = output.strip()
            clean_lines = [l.strip() for l in output.split('\n') if l.strip()]
            
            pid = None
            # Strategy 1: Look for a line that is purely digits (from the end, as echo $! is usually last)
            for line in reversed(clean_lines):
                if line.isdigit():
                    pid = line
                    break
            
            # Strategy 2: Fallback to regex search for any digits
            if not pid:
                matches = re.findall(r'\b\d+\b', output)
                if matches:
                    pid = matches[-1] # Take the last number found

            if pid and pid.isdigit():
                # Create Runtime
                runtime = get_runtime_manager().create_runtime(
                    name=f"Stress Test: {server.get('name', server_id)}",
                    module="server_stress",
                    context={
                        'server_id': server_id,
                        'cpu_cores': cpu_cores,
                        'cpu_load': cpu_load,
                        'vm_workers': vm_workers,
                        'vm_bytes': vm_bytes,
                        'io_workers': io_workers,
                        'timeout': timeout,
                        'pid': pid
                    }
                )
                get_runtime_manager().update_status(runtime.runtime_id, RuntimeStatus.RUNNING)

                with self._jobs_lock:
                    self.active_stress_jobs[server_id] = {
                        'status': 'running',
                        'pid': pid,
                        'start_time': time.time(),
                        'duration': timeout,
                        'runtime_id': runtime.runtime_id
                    }
                return True, f"Stress started (PID: {pid})"
            else:
                return False, f"Failed to get PID: {output}"
        return False, f"Failed to start stress: {output}"

    def stop_stress(self, server_id):
        with self._jobs_lock:
            job = self.active_stress_jobs.get(server_id)
        if not job:
            return False, "No active stress job found"
            
        server = self._get_server_by_id(server_id)
        if not server:
            return False, "Server not found"
            
        # Kill by PID
        pid = job['pid']
        cmd = f"kill -9 {pid}" # Force kill
        success, output = self._exec_ssh_command(server, cmd)
        
        if success:
            job['status'] = 'stopped'
            self._complete_job(server_id, job, RuntimeStatus.CANCELLED)
            return True, "Stress job stopped"
        return False, f"Failed to stop: {output}"

    def get_system_stats(self, server_id):
        """
        Get real-time system stats (CPU/Mem/IO)
        Returns: { 'cpu_usage': %, 'mem_usage': %, 'io_read_kb': 0, 'io_write_kb': 0 }
        """
        server = self._get_server_by_id(server_id)
        if not server:
            return None

        # 模拟从机
        if server.get('simulated') is True or server.get('ip') == 'sim://slave':
            import random
            if not hasattr(self, '_sim_slave_stats'):
                self._sim_slave_stats = {}
            s = self._sim_slave_stats.setdefault(server_id, {'cpu': 92, 'mem': 78, 'io_r': 0, 'io_w': 0})
            s['cpu'] = max(10, min(99, s['cpu'] + random.randint(-5, 5)))
            s['mem'] = max(20, min(95, s['mem'] + random.randint(-2, 2)))
            s['io_r'] = max(0, s['io_r'] + random.randint(-100, 200))
            s['io_w'] = max(0, s['io_w'] + random.randint(-100, 200))
            return {'cpu_usage': s['cpu'], 'mem_usage': s['mem'], 'io_read_kb': s['io_r'], 'io_write_kb': s['io_w']}
            
        # Real Server
        # 1. Get Memory (free -m)
        mem_cmd = "free -m | grep Mem"
        # 2. Get CPU & IO (vmstat 1 2)
        # vmstat output (usually):
        # r b swpd free buff cache si so bi bo in cs us sy id wa st
        # bi/bo = blocks in/out (approx KB/s)
        cpu_cmd = "vmstat 1 2 | tail -1"
        
        cmd = f"{mem_cmd} && {cpu_cmd}"
        
        success, output = self._exec_ssh_command(server, cmd, timeout=8)
        if not success:
            return None
            
        try:
            lines = output.strip().split('\n')
            if len(lines) < 2: return None
                
            # Parse Memory
            mem_parts = lines[0].split()
            total_mem = int(mem_parts[1])
            used_mem = int(mem_parts[2])
            mem_usage = round((used_mem / total_mem) * 100, 1) if total_mem > 0 else 0
            
            # Parse CPU & IO
            cpu_parts = lines[1].split()
            # Try to identify columns by counting from end (assuming st exists or not)
            # Standard: us sy id wa st (last 5) or us sy id wa (last 4)
            # bi bo are usually at index 8, 9 (if 17 cols) or close to middle.
            # Let's count from end: id is -3 (if st exists) or -2 (if no st).
            # Safest: check length.
            # Linux usually has 17 cols.
            
            # Robust way: vmstat usually has 'id' near end.
            # Let's assume last 5 are: us sy id wa st
            idle = int(cpu_parts[-3]) # id
            cpu_usage = 100 - idle
            
            # bi (blocks in) and bo (blocks out)
            # In standard output: 
            # 0 1 2    3    4    5     6  7  8  9  10 11 12 13 14 15 16
            # r b swpd free buff cache si so bi bo in cs us sy id wa st
            # bi is 8, bo is 9.
            # Relative to end: -9 and -8? 17-9=8.
            # Let's try flexible parsing or just fixed index 8/9 if len >= 10.
            io_read = 0
            io_write = 0
            if len(cpu_parts) >= 10:
                io_read = int(cpu_parts[8])
                io_write = int(cpu_parts[9])

            return {
                'cpu_usage': cpu_usage,
                'mem_usage': mem_usage,
                'io_read_kb': io_read,
                'io_write_kb': io_write
            }
        except Exception as e:
            logger.error(f"Stats parsing error: {e}")
            return None

    def check_oom_events(self, server_id):
        """
        Check dmesg for OOM killer events in the last minute.
        Returns list of event strings if found.
        """
        server = self._get_server_by_id(server_id)
        if not server:
            return []

        if server.get('simulated'):
            # Simulate OOM if simulated
            # Random chance or trigger? Let's just return empty unless triggered
            return []

        # Command to look for OOM in dmesg (last 100 lines should be enough for frequent polling)
        # Grep for "Out of memory" or "Kill process"
        cmd = "dmesg | tail -n 50 | grep -iE 'Out of memory|Kill process|oom-killer' || true"
        
        success, output = self._exec_ssh_command(server, cmd, timeout=5)
        if success and output:
            lines = output.strip().split('\n')
            # Deduplicate or process?
            # For now just return raw lines
            return lines
        return []

    def get_active_jobs(self):
        """返回当前运行中的压测任务列表 [{server_id, server_name, simulated}]，用于页面切换后恢复"""
        with self._jobs_lock:
            server_ids = [sid for sid, j in self.active_stress_jobs.items() if j.get('status') == 'running']
        jobs = []
        for server_id in server_ids:
            server = self._get_server_by_id(server_id)
            name = server.get('name', server_id) if server else server_id
            sim = server.get('simulated') is True or (server or {}).get('ip') in ('sim://slave', 'mock')
            jobs.append({'server_id': server_id, 'server_name': name, 'simulated': sim})
        return jobs

    def _get_server_by_id(self, server_id):
        if not self.ssh_manager:
            return None
        return self.ssh_manager.get_server_for_use(server_id)

    def _exec_ssh_command(self, server_info, command, timeout=10):
        if not self.ssh_manager:
            return False, "SSH Manager not initialized"
        success, output = self.ssh_manager.exec_command(server_info, command, timeout)
        if not success:
            return False, output
        # stress-ng/nohup 有时把提示写到 stderr，统一当成功时的输出
        return True, (output or "").strip()
