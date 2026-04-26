
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from modules.log_monitor.core.adb_controller import AdbController

class TestAdbControllerFps(unittest.TestCase):
    def setUp(self):
        self.controller = AdbController()
        self.controller.adb_path = "adb"
        self.controller.current_device_id = "test_device"

    @patch('subprocess.run')
    def test_collect_fps_fallback(self, mock_run):
        # 1. Mock standard gfxinfo returning NO profile data (simulating the failure case)
        # First call: dumpsys gfxinfo pkg_name (standard)
        # Second call: dumpsys gfxinfo pkg_name framestats (fallback)
        
        def side_effect(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "framestats" in cmd_str:
                # Mock framestats output
                # Format: Flags,IntendedVsync,Vsync,OldestInputEvent,NewestInputEvent,HandleInputStart,AnimationStart,PerformTraversalsStart,DrawStart,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,FrameCompleted
                # We care about Vsync (index 1)
                lines = ["Flags,IntendedVsync,Vsync,OldestInputEvent,NewestInputEvent,HandleInputStart,AnimationStart,PerformTraversalsStart,DrawStart,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,FrameCompleted"]
                
                # Generate 60 frames with 16.6ms interval (approx 60 FPS)
                start_time = 1000000000
                for i in range(60):
                    vsync = start_time + i * 16666666
                    lines.append(f"0,{vsync},{vsync},0,0,0,0,0,0,0,0,0,0,0")
                
                return MagicMock(stdout="\n".join(lines), returncode=0)
            else:
                # Mock standard gfxinfo with NO "Profile data in ms"
                return MagicMock(stdout="Applications Graphics Acceleration Info:\nUptime: 123456\nView hierarchy:\n  com.android.internal.policy.DecorView", returncode=0)

        mock_run.side_effect = side_effect
        
        # Run collection
        fps, jank, frames = self.controller._collect_fps_data("com.test.pkg")
        
        print(f"Collected FPS: {fps}")
        
        # Verify
        self.assertEqual(fps, 60)
        self.assertTrue(len(frames) > 0)

    @patch('subprocess.run')
    def test_collect_network_eth(self, mock_run):
        # Mock /proc/net/dev output with eth0
        mock_output = """
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
  eth0: 1000000       0    0    0    0     0          0         0 2000000       0    0    0    0     0       0          0
    lo:       0       0    0    0    0     0          0         0       0       0    0    0    0     0       0          0
"""
        mock_run.return_value = MagicMock(stdout=mock_output, returncode=0)
        
        # Set initial stats
        self.controller.last_net_stats = {'time': time.time() - 1.0, 'rx': 0, 'tx': 0}
        
        # Run collection
        rx, tx = self.controller._collect_network_data()
        
        print(f"Collected RX: {rx} KB/s, TX: {tx} KB/s")
        
        # 1000000 bytes / 1024 = 976.56 KB
        # 2000000 bytes / 1024 = 1953.12 KB
        self.assertAlmostEqual(rx, 976.56, delta=1.0)
        self.assertAlmostEqual(tx, 1953.12, delta=1.0)

if __name__ == '__main__':
    import time
    unittest.main()
