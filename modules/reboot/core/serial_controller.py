import threading
from .config import logger

class SerialController:
    def __init__(self, port='COM13', baudrate=9600):
        self.port = port
        self.baudrate = int(baudrate)
        self._ser = None
        self.last_tx = None

    def open(self):
        if self._ser:
            return
        try:
            import serial
            self._ser = serial.Serial(self.port, self.baudrate, timeout=1)
        except Exception as e:
            raise e

    def close(self):
        try:
            if self._ser:
                self._ser.close()
                self._ser = None
        except Exception:
            pass

    def _send(self, tx_bytes):
        self.last_tx = ' '.join(f"{b:02x}" for b in tx_bytes)
        logger.info(f"[串口] 发送码值: {self.last_tx}")
        if self._ser:
            self._ser.write(bytes(tx_bytes))

    def _get_addr_int(self, addr):
        return int(addr, 16) if isinstance(addr, str) else int(addr)

    def power_on(self, channel: int, address):
        addr_int = self._get_addr_int(address)
        tx = [0x55, addr_int & 0xFF, 0x00, int(channel) & 0xFF, 0xF0, 0xAA]
        self._send(tx)

    def power_off(self, channel: int, address):
        addr_int = self._get_addr_int(address)
        tx = [0x55, addr_int & 0xFF, 0x00, int(channel) & 0xFF, 0xF1, 0xAA]
        self._send(tx)


CTRL_POOL = {}
CTRL_LOCKS = {}
POOL_GUARD = threading.Lock()


def get_controller(com_port: str, baud: int, addr_hex: str):
    # Key by port and baud only, so devices with different addresses share the same port controller
    key = (com_port, int(baud))
    with POOL_GUARD:
        entry = CTRL_POOL.get(key)
        if not entry:
            ctrl = SerialController(port=com_port, baudrate=int(baud))
            lock = threading.Lock()
            CTRL_POOL[key] = ctrl
            CTRL_LOCKS[key] = lock
        return CTRL_POOL[key], CTRL_LOCKS[key]
