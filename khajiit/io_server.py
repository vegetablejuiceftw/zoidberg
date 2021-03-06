# ROS setup before gevent
import messenger
from config_manager import ConfigManager
from websocket_log_handler import WebsocketLogHandler

movement_publisher = messenger.Publisher('/movement', messenger.Messages.motion)
kicker_publisher = messenger.Publisher('/kicker_speed', messenger.Messages.integer)
command_publisher = messenger.Publisher('/command', messenger.Messages.string)
strategy_state = messenger.Listener('/strategy', messenger.Messages.string)
canbus_state = messenger.Listener('/canbus_message', messenger.Messages.string)
websocket_log_handler = WebsocketLogHandler()
logging_state = messenger.Listener('/rosout_agg', messenger.Messages.logging, callback=websocket_log_handler.emit)
node = messenger.Node('io_server', disable_signals=True)

# thread fixes
import gevent
from gevent import monkey

monkey.patch_all(thread=False)

# imports
import json
from time import time

from flask import Flask, render_template, request, redirect
from flask_sockets import Sockets


logger = node.logger

app = Flask(__name__)

try:
    with open("/etc/machine-id", "r") as fh:
        app.config['SECRET_KEY'] = fh.read()
except:
    app.config['SECRET_KEY'] = 'secret!'

ip, port = ('0.0.0.0', 5000)
# TODO: maybe useful
# if os.getuid() == 0:
#     port = 80

sockets = Sockets(app)

# Queue messages from bootstrap
websockets = websocket_log_handler.websockets


@app.route('/')
def group():
    return render_template('group.html')


@app.route('/logging')
def logging_view():
    return render_template('logging.html')

# redirect to image server
@app.route('/combined/<path:type_str>')
def video_combined(type_str):
    url = request.url
    url = url.replace(str(port), '5005')
    return redirect(url)


@sockets.route('/')
def command(websocket):
    # send logging history to ws
    for buf in list(websocket_log_handler.queue):
        websocket.send(buf)

    def send_settings_packet():
        game_config = ConfigManager.get_value('game')
        if not game_config:
            ConfigManager.set_value('game|global|field_id', 'A')
            ConfigManager.set_value('game|global|robot_id', 'A')
            ConfigManager.set_value('game|global|target goal color', 'blue')
            ConfigManager.set_value('game|global|gameplay status', 'disabled')

        game_options = [
            ("field_id", [ConfigManager.get_value("game|global|field_id"), "A", "B", "Z"]),
            ("robot_id", [ConfigManager.get_value("game|global|robot_id"), "A", "B"]),
            ("target goal color", [ConfigManager.get_value("game|global|target goal color"), "purple", "blue"]),
            ("gameplay status", [ConfigManager.get_value("game|global|gameplay status"), "disabled", "enabled"]),
        ]

        settings_packet = json.dumps(dict(
            action="settings-packet",
            sliders=dict(color=ConfigManager.get_value("color")),
            options=game_options
        ))
        websocket.send(settings_packet)

    send_settings_packet()

    last_press = time()
    last_press_history = []

    counter = 0
    rpm = 1000
    while not websocket.closed:
        counter += 1
        websockets.add(websocket)

        gevent.sleep(0.005)

        msg = websocket.receive()

        if not msg:
            websockets.remove(websocket)
            logger.error("WebSocket connection presumably closed, %d left connected" % len(websockets))
            break

        response = json.loads(msg)

        action = response.pop("action", None)

        game_package = strategy_state.package or {}
        canbus_package = canbus_state.package or {}

        gameplay_status = game_package.get('is_enabled', None)
        target_goal_angle = round(game_package.get('target_goal_angle') or 0, 3)
        canbus_rpm = canbus_package.get('last_rpm') or 0
        closest_ball = game_package.get('closest_ball') or {}
        dist = game_package.get('dist') or 0

        if action == "gamepad":
            controls = response.get("data")
            # print(controls)

            if controls:
                toggle_gameplay = controls.get("controller0.button8", controls.get("controller0.button11", None))
                if toggle_gameplay is False:  # False is key up event
                    ConfigManager.set_value('game|global|gameplay status', 'disabled' if gameplay_status else 'enabled')

                elif not gameplay_status:
                    # # Manual control of the robot
                    x = controls.get("controller0.axis0", 0) * 0.33
                    y = controls.get("controller0.axis1", 0) * 0.33
                    w = controls.get("controller0.axis3", 0) * 0.2

                    if controls.get("controller0.button3"):
                        y = 0.15

                    if x or y or w:
                        movement_publisher.publish(x=x, y=-y, az=-w)

                    delta = controls.get("controller0.button12", 0)
                    delta = delta or -controls.get("controller0.button13", 0)

                    if delta:
                        rpm = max(0, min(rpm + delta * 50, 15000))
                        logger.info(f"PWM+: {rpm:.0f}")

                    if controls.get("controller0.button0", None):
                        logger.info_throttle(1, f"drive_towards_target_goal: {target_goal_angle} rpm:{rpm} speed:{canbus_rpm}")
                        kicker_publisher.publish(rpm)
                        # no driving backwards when angle error
                        command_publisher.command(drive_towards_target_goal=dict(backtrack=False, speed_factor=0.8))

                    if controls.get("controller0.button5", None):
                        logger.info_throttle(0.3, f"kick: {target_goal_angle} rpm:{rpm} speed:{canbus_rpm}")
                        kicker_publisher.publish(rpm)

                    if controls.get("controller0.button6", None):
                        logger.info_throttle(1, "Drive to center")
                        command_publisher.command(drive_to_field_center=None)

                    if controls.get("controller0.button7", None):
                        logger.info_throttle(1, f"Flank {target_goal_angle} {closest_ball.get('dist')} {closest_ball.get('angle_deg')}")
                        command_publisher.command(flank=None)

                    if controls.get("controller0.button4", None):
                        logger.info_throttle(1, f"GmaeShoot {target_goal_angle:.1f} {dist:.0f} {canbus_rpm:.0f}")
                        command_publisher.command(kick=None)

                    if controls.get("controller0.button2", None):
                        logger.info_throttle(1, f"align_to_goal: {target_goal_angle} speed:{canbus_rpm}")
                        command_publisher.command(align_to_goal=dict(factor=1))

                last_press_history = [*last_press_history, time() - last_press][-30:]
                last_press = time()
                if counter % 30 == 1:
                    average = sum(last_press_history) / len(last_press_history)
                    if not gameplay_status:
                        format = dict((k, v if type(v) == bool else round(v, 1)) for k, v in controls.items())
                        logger.info_throttle(5, f"Last press {average:.3f} ago on average, {format}")

        elif action == "set_settings":
            for k, v in response.items():
                ConfigManager.set_value(k, v)
            send_settings_packet()
        elif action == "set_options":
            for k, v in response.items():
                ConfigManager.set_value(f"game|global|{k}", v)
            send_settings_packet()
        elif action == "ping":
            try:
                send_settings_packet()
            except Exception as e:
                logger.info("Socket is dead")

        else:
            logger.error("Unhandled action: %s", str(action))

    websockets.remove(websocket)
    logger.info("WebSocket connection closed, %d left connected", len(websockets))
    return b""


def main():
    from gevent import pywsgi
    from geventwebsocket.handler import WebSocketHandler

    messenger.ConnectPythonLoggingToROS.reconnect('config_manager')

    logger.info("Starting robovision")

    server = pywsgi.WSGIServer((ip, port), app, handler_class=WebSocketHandler)
    logger.info("Started server at http://{}:{}".format(ip, port))

    server.serve_forever()


if __name__ == '__main__':
    main()
