import threading
from time import time

import uavcan
import yaml


class CanBusMotor:
    # TODO: this has the ability to not constantly update the speed, we should use that

    SERIAL = '/dev/serial/by-id/usb-Zubax_Robotics_Zubax_Babel_32002E0018514D563935392000000000-if00'

    def __init__(self) -> None:
        self.last_raw = ""
        self.last_msg = {}
        self.last_rpm = 0
        self.rpm = []
        self._speed = 0
        self.last_edit = time()

        self.node = node = uavcan.make_node(
            self.SERIAL,
            node_id=10,
            bitrate=1000000,
        )

        # setup
        node_monitor = uavcan.app.node_monitor.NodeMonitor(node)
        dynamic_node_id_allocator = uavcan.app.dynamic_node_id.CentralizedServer(node, node_monitor)

        # Waiting for at least one other node to appear online (our local node is already online).
        while len(dynamic_node_id_allocator.get_allocation_table()) <= 1:
            print('Waiting for other nodes to become online...')
            node.spin(timeout=1)

        node.periodic(0.08, self.update)
        node.add_handler(uavcan.equipment.esc.Status, self.listen)

        self.thread = threading.Thread(target=node.spin, daemon=True)
        self.thread.start()

    @property
    def speed(self):
        return self.__speed

    @speed.setter
    def speed(self, speed):
        self._speed = speed
        self.last_edit = time()

    def update(self):
        if time() - self.last_edit > 0.8:
            if self._speed:
                print(self.last_raw)
            self._speed = 0

        message = uavcan.equipment.esc.RawCommand(cmd=[self._speed])
        self.node.broadcast(message)

    def listen(self, msg):
        """
        Transfer(
            id=4, source_node_id=125, dest_node_id=None, transfer_priority=7,
            payload=uavcan.equipment.esc.Status(
                error_count=0, voltage=12.9296875,
                current=-0.0, temperature=307.0, rpm=0, power_rating_pct=0, esc_index=0)
            )
        """

        self.last_raw = uavcan.to_yaml(msg)
        self.last_msg = yaml.load(self.last_raw)
        self.rpm = (self.rpm + [self.last_msg.get('rpm', 0)])[-10:]
        self.last_rpm = round(sum(self.rpm) / len(self.rpm))


if __name__ == '__main__':
    kicker = CanBusMotor()

    while True:
        try:
            speed = int(input("speed?: "))
            kicker.speed = speed
        except ValueError:
            pass

        print(kicker.last_raw)