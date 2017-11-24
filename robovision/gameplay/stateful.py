import logging

from line_fit import dist_to_pwm

logger = logging.getLogger("gameplay")
from managed_threading import ManagedThread
from image_recognition import Point, PolarPoint
from controller import Controller
import math
from time import time

from collections import defaultdict
from config_manager import ConfigManager

config = ConfigManager("game")


def distance(point_a, point_b):
    A = point_a.x * point_a.dist, point_a.y * point_a.dist
    B = point_b.x * point_b.dist, point_b.y * point_b.dist
    dist = ((A[0] - B[0]) ** 2 + (A[1] - B[1]) ** 2) ** 0.5
    return dist


class Gameplay(ManagedThread):
    def __init__(self, *args, **kwargs):
        ManagedThread.__init__(self, *args)
        self.arduino = Controller()  # config read from ~/.robovision/pymata.conf
        self.state = Patrol(self)
        self.recognition = None
        self.closest_edges = []
        self.safe_distance_to_goals = 1.4
        self.config = config

        self.target_goal_distances = [100]
        self.target_goal_distance = 100

        self.last_kick = time()

        self.recent_closest_balls = []

    def field_id(self):
        return self.config.get_option("global", "field_id", type=str, default='A').get_value()

    @property
    def robot_id(self):
        return self.config.get_option("global", "robot_id", type=str, default='A').get_value()

    @property
    def is_enabled(self):
        return self.config.get_option("global", "gameplay status", type=str,
                                      default='disabled').get_value() == 'enabled'

    @property
    def config_goal(self):
        return self.config.get_option("global", "target goal color", type=str, default='blue').get_value()

    @property
    def balls(self):
        balls = []
        goals = []
        if self.own_goal:
            goals.append(self.own_goal)
        if self.target_goal:
            goals.append(self.target_goal)

        too_close = []
        suspicious = []
        for ball in self.recognition.balls:
            if ball[0].suspicious:
                suspicious.append(ball)
                continue

            # for goal in goals:
            #     if distance(ball[0], goal) < 0.3:
            #         too_close.append(ball)
            #         break
            # else:
            balls.append(ball)

        if len(self.recognition.balls) != len(balls + too_close + suspicious):
            logger.info("FUUUCK")

        # logger.info("Normal{} too_cose{} suspicious{}".format(len(balls), len(too_close), len(suspicious)))

        # logger.info("WASD{} {}".format(len(self.recognition.balls), len(balls + too_close)))
        return balls + too_close + suspicious

    @property
    def own_goal(self):
        return self.recognition.goal_yellow if self.config_goal == 'blue' else self.recognition.goal_blue

    @property
    def target_goal(self):
        return self.recognition.goal_blue if self.config_goal == 'blue' else self.recognition.goal_yellow

    @property
    def has_ball(self):
        return self.arduino.has_ball

    @property
    def target_goal_angle(self):
        if self.target_goal:
            return self.target_goal.angle_deg

    @property
    def target_goal_dist(self):
        if self.target_goal:
            return self.target_goal.dist

    @property
    def too_close(self):
        min_dist = 0.35
        if self.target_goal and self.target_goal.dist < min_dist:
            return True
        if self.own_goal and self.own_goal.dist < min_dist:
            return True

    @property
    def alligned(self):
        # TODO: inverse of this
        if self.target_goal:
            min_angle = 2 if not self.target_goal_dist or self.target_goal_dist > 3 else 3
            return abs(self.target_goal_angle) <= min_angle

    @property
    def focus_time(self):
        return 0.1

    @property
    def rest_time(self):
        return 0.1

    @property
    def closest_edge(self):
        if not self.recognition.closest_edge:
            return
        self.closest_edges = self.closest_edges[1:10] + [self.recognition.closest_edge]
        x, y, = 0, 0
        for closest_edge in self.closest_edges:
            x += closest_edge.x
            y += closest_edge.y
        length = (x ** 2 + y ** 2) ** 0.5
        return x / length, y / length, length

    @property
    def field_center_angle(self):
        x, y = self.closest_edge[:2]
        angle = math.atan2(-x, -y)
        # logger.info("{:.1f} {:.1f} {:.1f}".format(x,y, angle))

        return Point(-x, -y).angle_deg

    @property
    def flank_is_alligned(self):
        # if self.balls and abs(self.balls[0][0].angle_deg) < 10:
        #     return True

        in_line = self.goal_to_ball_angle

        if in_line and abs(in_line) < 13:
            log_str = "B{:.1f} G{:.1f} | D{:.1f}".format(self.balls[0][0].angle_deg, self.target_goal.angle_deg,
                                                         in_line)
            # logger.info(log_str)

            return True

    @property
    def goal_to_ball_angle(self):
        if not self.target_goal or not self.balls:
            logger.info("goal_to_ball_angle failed: GOAL:{} BAALS:{}".format(self.target_goal, self.balls))
            return

        ball = self.balls[0][0]
        goal = self.target_goal

        vg = goal.angle_deg
        vb = ball.angle_deg

        r = vb - vg

        if r > 180:
            r -= 360
        if r < -180:
            r += 360
        return r  # degrees

    @property
    def too_close_to_edge(self):
        edge = self.closest_edge
        return edge[2] < 0.4

    @property
    def closest_goal_distance(self):
        own, other = self.own_goal, self.target_goal
        distances = []
        if own:
            distances.append(own.dist)
        if other:
            distances.append(other.dist)
        if own or other:
            return min(distances)
        return 0

    @property
    def danger_zone(self):
        edge = self.closest_edge
        return edge[2] < 1.1 or self.closest_goal_distance < 1

    @property
    def blind_spot_for_shoot(self):
        return (not self.own_goal or self.own_goal.dist > 3.0) and self.closest_edge[2] < 1.2

    def drive_towards_target_goal(self):
        rotation = self.rotation_for_goal() or 0
        angle = self.target_goal_angle or 0
        if abs(angle) > 2:
            return self.arduino.set_xyw(0, -0.16, 0)
        factor = abs(math.tanh(angle / 18))
        return self.arduino.set_xyw(0, 0.13, rotation * (factor * 2))

    def rotate(self, degrees):
        delta = degrees / 360
        # TODO enable full rotating
        self.arduino.set_xyw(0, 0, -delta)

    def drive_xy(self, x, y):
        self.arduino.set_xyw(y, x, 0)

    def flank_vector(self):
        angle = self.goal_to_ball_angle
        if angle is None:
            logger.info("not flank vector")
            return

        ball = self.average_closest_ball or self.balls[0][0]

        dist = ball.dist

        bx, by = ball.x / dist, ball.y / dist
        if dist > 0.53:
            return bx * 0.6, by * 0.6

        sign = [-1, 1][angle > 0]

        factor = abs(math.tanh(angle / 30))
        print(factor)

        delta_deg = abs(angle) * 2 + 10

        delta_deg += abs(angle) * factor

        delta_deg = min(delta_deg, 80)
        delta_deg *= sign

        delta = math.radians(delta_deg)

        # rotate ball vector towards desired position
        x = bx * math.cos(delta) - by * math.sin(delta)
        y = bx * math.sin(delta) + by * math.cos(delta)

        factor = abs(math.tanh(angle / 60)) + 0.2
        # TODO: falloff when goal angle and dist decreases
        return round(x * factor * 0.7, 6), round(y * factor * 0.7, 6)

    def rotation_for_goal(self):
        goal_angle = self.target_goal_angle
        if goal_angle is not None:
            maximum = 50
            angle = min(goal_angle, maximum)
            factor = abs(math.tanh(angle / 30))
            rotate = -angle * factor / maximum
            rotate = max(0.05, abs(rotate)) * [-1, 1][rotate > 0]
            # print('rotate %.02f %.02f %.02f' % (angle, factor, rotate))
            return rotate

    def flank(self):
        rotation = self.rotation_for_goal() or 0

        goal_angle = self.target_goal_angle
        shooting_angle = self.goal_to_ball_angle or 999

        if goal_angle is None:
            return

        if abs(goal_angle) > max(abs(shooting_angle * 3), 10):
            print("rotate", goal_angle, shooting_angle)
            return self.arduino.set_xyw(0, 0, rotation)

        flank = self.flank_vector()

        if not flank:
            print('flank', flank)
            return
        x, y = flank

        self.arduino.set_xyw(y, x, rotation / 1.4)

    @property
    def continue_to_kick(self):
        return time() - self.last_kick < 1

    def kick(self, update=True):
        if update:
            self.last_kick = time()

        if self.target_goal_distance and self.continue_to_kick:
            pwm = dist_to_pwm(self.target_goal_distance)
            return self.arduino.set_thrower(pwm)
        if not self.target_goal_distance:
            print("derp")

    def stop_moving(self):
        self.arduino.set_abc(0, 0, 0)

    def drive_to_ball(self):
        if not self.balls:
            return

        ball = self.balls[0][0]
        # logger.info("%f" % ball.dist)

        x, y = ball.x, ball.y
        min_speed = 0.3
        max_val = max([abs(x), abs(y)])
        if max_val < min_speed and max_val:
            scaling = min_speed / max_val
            x *= scaling
            y *= scaling

        w = - ball.angle_deg / 180.0

        self.arduino.set_xyw(y, x, w)

    def drive_away_from_goal(self):

        # logger.info(str([self.own_goal]))
        if self.own_goal and self.target_goal:
            goal = self.own_goal if self.own_goal.dist > self.target_goal.dist else self.target_goal
        else:
            goal = self.own_goal or self.target_goal
        if not goal:
            return

        x, y = goal.x, goal.y

        if goal.dist < 1.5:  # self.safe_distance_to_goals:
            x *= -1
            y *= -1

        self.arduino.set_xyw(y, x, 0.5)

    def drive_to_field_center(self):

        if not self.closest_edge:
            return

        x, y, _ = self.closest_edge

        # logger.info("{} == {}?".format(self.field_center_angle, Point(-x, -y).angle_deg))
        self.arduino.set_xyw(-y / 3, -x / 3, 0)

    @property
    def last_closest_ball(self) -> PolarPoint:
        return self.recent_closest_balls[0] if self.recent_closest_balls else None

    def update_recent_closest_balls(self):
        if self.closest_ball and self.closest_ball.dist < 0.5 and self.closest_ball.angle_deg_abs < 15:
            self.recent_closest_balls = [self.closest_ball] + self.recent_closest_balls[:12]
        else:
            self.recent_closest_balls = self.recent_closest_balls[:-1]

    @property
    def closest_ball(self) -> PolarPoint:
        if self.balls:
            return self.balls[0][0]

    @property
    def average_closest_ball(self) -> PolarPoint:
        if not self.recent_closest_balls:
            return

        A, D = [], []
        for b in self.recent_closest_balls:
            A.append(b.angle_rad)
            D.append(b.dist)
        a = sum(A) / len(A)
        d = sum(D) / len(D)
        return PolarPoint(a, d)

    def step(self, recognition, *args):
        self.arduino.set_abc(0, 0, 0)
        if not recognition:
            return
        self.recognition = recognition

        if self.target_goal:
            self.target_goal_distances = [self.target_goal.dist * 100] + self.target_goal_distances[:10]
            self.target_goal_distance = sum(self.target_goal_distances) / len(self.target_goal_distances)

        self.state = self.state.tick()

        self.kick(update=False)

        self.update_recent_closest_balls()

        self.arduino.apply()

    def on_enabled(self, *args):
        self.config.get_option("global", "gameplay status", type=str, default='disabled').set_value('enabled')
        self.arduino.set_grabber(True)

    def on_disabled(self, *args):
        self.config.get_option("global", "gameplay status", type=str, default='disabled').set_value('disabled')
        self.arduino.set_grabber(False)
        self.arduino.set_xyw(0, 0, 0)

    def start(self):
        self.arduino.start()
        self.state = Flank(self)
        ManagedThread.start(self)


class StateNode:
    is_recovery = False
    recovery_counter = 0
    recovery_factor = 0.5

    def __init__(self, actor: Gameplay):
        self.transitions = dict(self.exctract_transistions())
        self.actor = actor
        self.time = time()
        self.timers = defaultdict(time)

    @property
    def elapsed(self):
        return time() - self.time

    @property
    def forced_recovery_time(self):
        logger.info("DEBUG forced_recovery_time: %f" % (time() - self.time))
        return self.time + min(self.recovery_counter * self.recovery_factor, 5) > time()

    def exctract_transistions(self):
        return [i for i in self.__class__.__dict__.items() if 'VEC' in i[0]]

    def transition(self):
        for name, vector in self.transitions.items():
            result = vector(self)
            if result:
                logger.info("\n%s --> %s" % (name, result.__class__.__name__))
                return result
        return self

    def animate(self):
        print("I exist")

    def tick(self):
        next_state = self.transition() or self
        if next_state != self:
            StateNode.recovery_counter += next_state.is_recovery
            # self.actor.stop_moving()  # PRE EMPTIVE, should not cause any issues TM
            # logger.info(str(next_state))
        else:  # TODO: THis causes latency, maybe ?
            self.animate()
        return next_state

    def __str__(self):
        return str(self.__class__.__name__)


class RetreatMixin(StateNode):
    pass
    # TODO: enable once ready for battle
    # def VEC_TOO_CLOSE(self):
    #     if self.actor.too_close:
    #         return Penalty(self.actor)
    #
    # def VEC_TOO_CLOSE_TO_EDGE(self):
    #     if self.actor.too_close_to_edge:
    #         return OutOfBounds(self.actor)


class DangerZoneMixin(StateNode):
    def VEC_IN_DANGER_ZONE(self):
        if self.actor.danger_zone and self.actor.balls:
            return Drive(self.actor)


class HasBallMixin(StateNode):
    def VEC_HAS_BALL(self):
        if self.actor.has_ball:
            return FindGoal(self.actor)


class Patrol(RetreatMixin, HasBallMixin, StateNode):
    def animate(self):
        self.actor.drive_to_field_center()

    def VEC_SEE_BALLS_AND_CAN_FLANK(self):
        if self.actor.balls and not self.actor.danger_zone:
            return Flank(self.actor)

    def VEC_SEE_BALLS_AND_SHOULD_DRIVE(self):
        if self.actor.balls and self.actor.danger_zone:
            return Flank(self.actor)


class Flank(HasBallMixin, RetreatMixin, DangerZoneMixin, StateNode):
    def animate(self):
        self.actor.flank()
        self.actor.kick()

    def VEC_SHOULD_SHOOT(self):
        last_best_ball = self.actor.average_closest_ball
        if not last_best_ball:
            return

        if last_best_ball.angle_deg_abs < 9 and last_best_ball.dist < 0.22:
            print(self.actor.target_goal_distance)
            return Shoot(self.actor)

        if last_best_ball.angle_deg_abs < 4.5 and last_best_ball.dist < 0.3:
            print(self.actor.target_goal_distance, 'w2')
            return Shoot(self.actor)

    def VEC_NO_BALLS(self):
        if not self.actor.balls:
            return Patrol(self.actor)

    def VEC_LOST_GOAL(self):
        if not self.actor.target_goal:
            return Patrol(self.actor)


class Shoot(StateNode):
    def animate(self):
        self.actor.drive_towards_target_goal()
        self.actor.kick()

    def VEC_DONE_SHOOT(self):
        if self.elapsed > 2:
            return Flank(self.actor)


class Drive(HasBallMixin, RetreatMixin, StateNode):
    def animate(self):
        self.actor.drive_to_ball()
        self.actor.kick()

    def VEC_HAS_BALL_IN_BLIND_SPOT(self):
        if self.actor.has_ball and self.actor.blind_spot_for_shoot:
            pass
            # return OutOfBounds(self.actor)

    def VEC_LOST_SIGHT(self):
        if not self.actor.has_ball and not self.actor.balls:
            return Patrol(self.actor)


class FindGoal(StateNode):
    def animate(self):
        self.actor.drive_to_field_center()

    def VEC_HAS_GOAL(self):
        if self.actor.target_goal:
            return TargetGoal(self.actor)

    def VEC_LOST_BALL(self):
        if not self.actor.has_ball:
            return Patrol(self.actor)


class DriveToCenter(RetreatMixin, StateNode):
    is_recovery = True

    def animate(self):
        self.actor.drive_to_field_center()

    def VEC_LOST_BALL(self):
        if not self.actor.has_ball:
            return Patrol(self.actor)

    def VEC_IN_CENTER(self):
        if self.time + 1.5 > time():
            return TargetGoal(self.actor)


class TargetGoal(RetreatMixin, StateNode):
    # instead drive infront of own goal to fuck around with opponnentos

    VISITS = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        TargetGoal.VISITS.append(time())
        TargetGoal.VISITS = list(filter(lambda x: x + 0.5 > time(), TargetGoal.VISITS))

    def animate(self):
        self.actor.drive_towards_target_goal()
        self.actor.kick()
        # self.actor.drive_away_from_goal()

    def VEC_TOO_MANY_VISITS(self):
        # return
        if len(TargetGoal.VISITS) > 4:
            return DriveToCenter(self.actor)

    def VEC_POINTED_AT_GOAL(self):
        # return
        if self.actor.alligned:
            return Focus(self.actor)

    def VEC_LOST_BALL(self):
        if not self.actor.has_ball:
            return Patrol(self.actor)

    def VEC_LOST_GOAL(self):
        if not self.actor.target_goal:
            return FindGoal(self.actor)


class Focus(StateNode):
    def animate(self):
        self.actor.stop_moving()
        self.actor.kick()

    def VEC_NOT_ALLIGNED(self):
        if not self.actor.alligned:
            return TargetGoal(self.actor)

    def VEC_READY_TO_SHOOT(self):
        if self.actor.alligned:
            return Drive(self.actor)


class OutOfBounds(StateNode):
    is_recovery = True

    def animate(self):
        if self.actor.has_ball and abs(self.actor.field_center_angle) > 15 and self.time + 0.5 > time():
            self.actor.rotate(self.actor.field_center_angle)
        else:
            self.actor.drive_to_field_center()

    def VEC_DONE_CENTERING(self):
        _, _, length = self.actor.closest_edge
        if length > 1.2 and not self.forced_recovery_time:
            return Patrol(self.actor)


class Penalty(StateNode):
    VISITS = []
    is_recovery = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        Penalty.VISITS.append(time())
        Penalty.VISITS = list(filter(lambda x: x + 2 > time(), Penalty.VISITS))

    def animate(self):
        self.actor.drive_away_from_goal()

    def VEC_ENOUGH_FAR(self):
        own, other = self.actor.own_goal, self.actor.target_goal
        safe_dist = self.actor.safe_distance_to_goals
        if (not own or own.dist >= safe_dist) and (
                    not other or other.dist >= safe_dist) and not self.forced_recovery_time:
            return Patrol(self.actor)

    def VEC_TOO_CLOSE_TO_EDGE(self):
        if self.actor.too_close_to_edge:
            return OutOfBounds(self.actor)
