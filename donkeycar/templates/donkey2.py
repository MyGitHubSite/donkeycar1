#!/usr/bin/env python3
"""
Scripts to drive a donkey 2 car and train a model for it. 

Usage:
    manage.py (drive) [--model=<model>] [--js]
    manage.py (drive_fuzzy) [--model=<model>] [--js]
    manage.py (train) [--tub=<tub1,tub2,..tubn>]  (--model=<model>) [--no_cache]

Options:
    -h --help        Show this screen.
    --tub TUBPATHS   List of paths to tubs. Comma separated. Use quotes to use wildcards. ie "~/tubs/*"
    --js             Use physical joystick.
"""
import os
from docopt import docopt

import donkeycar as dk

#import parts
from donkeycar.parts.transform import Lambda
from donkeycar.parts.keras import KerasFuzzyAndUltrasonicSensors, KerasUltrasonicSensors
from donkeycar.parts.datastore import TubHandler, TubGroup
from donkeycar.parts.controller import LocalWebController, JoystickController
import numpy as np
import tensorflow as tf


def drive(cfg, model_path=None, use_joystick=False, use_fuzzy=False):
    from donkeycar.parts.camera import PiCamera
    from donkeycar.parts.ultrasonic import Ultrasonic, MockUltrasonic, CacheUltrasonicClient
    from donkeycar.parts.actuator import PCA9685, PWMSteering, PWMThrottle
    from donkeycar.parts.obstacle import Obstacle
    '''
    Construct a working robotic vehicle from many parts.
    Each part runs as a job in the Vehicle loop, calling either
    it's run or run_threaded method depending on the constructor flag `threaded`.
    All parts are updated one after another at the framerate given in
    cfg.DRIVE_LOOP_HZ assuming each part finishes processing in a timely manner.
    Parts may have named outputs and inputs. The framework handles passing named outputs
    to parts requesting the same named input.
    '''

    #Initialize car
    V = dk.vehicle.Vehicle()
    cam_front = PiCamera(resolution=cfg.CAMERA_RESOLUTION, name = 'front')
    V.add(cam_front, outputs=['cam/image_array'], threaded=True)

    us_front = CacheUltrasonicClient(gpio_trigger=cfg.ULTRASONIC_FRONT_TRIGGER, gpio_echo=cfg.ULTRASONIC_FRONT_ECHO, poll_delay = 0.05, name='front')
    V.add(us_front, outputs=['ultrasonic_front/distance'], threaded=True)
	
    us_front_left = CacheUltrasonicClient(gpio_trigger=cfg.ULTRASONIC_FRONT_LEFT_TRIGGER, gpio_echo=cfg.ULTRASONIC_FRONT_LEFT_ECHO, poll_delay = 0.05, name='front_left')
    V.add(us_front_left, outputs=['ultrasonic_front_left/distance'], threaded=True)
	
    us_front_right = CacheUltrasonicClient(gpio_trigger=cfg.ULTRASONIC_FRONT_RIGHT_TRIGGER, gpio_echo=cfg.ULTRASONIC_FRONT_RIGHT_ECHO, poll_delay = 0.05, name='front_right')
    V.add(us_front_right, outputs=['ultrasonic_front_right/distance'], threaded=True)
	
    if use_joystick or cfg.USE_JOYSTICK_AS_DEFAULT:
        #modify max_throttle closer to 1.0 to have more power
        #modify steering_scale lower than 1.0 to have less responsive steering
        ctr = JoystickController(max_throttle=cfg.JOYSTICK_MAX_THROTTLE,
                                 steering_scale=cfg.JOYSTICK_STEERING_SCALE,
                                 auto_record_on_throttle=cfg.AUTO_RECORD_ON_THROTTLE)
    else:        
        #This web controller will create a web server that is capable
        #of managing steering, throttle, and modes, and more.
        ctr = LocalWebController()
    
    V.add(ctr, 
          inputs=['cam/image_array', 'ultrasonic_front/distance', 'ultrasonic_front_left/distance', 'ultrasonic_front_right/distance', 'pilot/action', 'pilot/angle', 'pilot/throttle'],
          outputs=['user/angle', 'user/throttle', 'user/mode', 'recording'],
          threaded=True)

    #See if we should even run the pilot module. 
    #This is only needed because the part run_condition only accepts boolean
    def pilot_condition(mode):
        if mode == 'user':
            return False
        else:
            return True
        
    pilot_condition_part = Lambda(pilot_condition)
    V.add(pilot_condition_part, inputs=['user/mode'], outputs=['run_pilot'])
	
    #Obstacle detection
    ob = Obstacle()
    
    V.add(ob, 
          inputs=['cam/image_array', 'ultrasonic_front/distance', 'ultrasonic_front_left/distance', 'ultrasonic_front_right/distance'],
          outputs=['pilot/action'])
    
    #Run the pilot if the mode is not user.
    if use_fuzzy:
        kl = KerasFuzzyAndUltrasonicSensors()
    else:
        kl = KerasUltrasonicSensors()
		
    if model_path:
        kl.load(model_path)
    
    V.add(kl, 
          inputs=['cam/image_array', 'ultrasonic_front/distance', 'ultrasonic_front_left/distance', 'ultrasonic_front_right/distance', 'pilot/action'],
          outputs=['pilot/angle', 'pilot/throttle'],
          run_condition='run_pilot', threaded = True)
    
    #Choose what inputs should change the car.
    def drive_mode(mode, 
                   user_angle, user_throttle,
                   pilot_angle, pilot_throttle):
        if mode == 'user': 
            return user_angle, user_throttle
        
        elif mode == 'local_angle':
            return pilot_angle, user_throttle
        
        else: 
            return pilot_angle, pilot_throttle
        
    drive_mode_part = Lambda(drive_mode)
    V.add(drive_mode_part, 
          inputs=['user/mode', 'user/angle', 'user/throttle',
                  'pilot/angle', 'pilot/throttle'], 
          outputs=['angle', 'throttle'])
    
    
    steering_controller = PCA9685(cfg.STEERING_CHANNEL)
    steering = PWMSteering(controller=steering_controller,
                                    left_pulse=cfg.STEERING_LEFT_PWM, 
                                    right_pulse=cfg.STEERING_RIGHT_PWM)
    
    throttle_controller = PCA9685(cfg.THROTTLE_CHANNEL)
    throttle = PWMThrottle(controller=throttle_controller,
                                    max_pulse=cfg.THROTTLE_FORWARD_PWM,
                                    zero_pulse=cfg.THROTTLE_STOPPED_PWM, 
                                    min_pulse=cfg.THROTTLE_REVERSE_PWM)
    
    V.add(steering, inputs=['angle'])
    V.add(throttle, inputs=['throttle'])
    
    #add tub to save data
    inputs=['cam/image_array', 'ultrasonic_front/distance', 'ultrasonic_front_left/distance', 'ultrasonic_front_right/distance', 'user/angle', 'user/throttle', 'user/mode']
    types=['image_array', 'float', 'float', 'float', 'float', 'float', 'str']
    
    th = TubHandler(path=cfg.DATA_PATH)
    tub = th.new_tub_writer(inputs=inputs, types=types)
    V.add(tub, inputs=inputs, run_condition='recording')
    
    #run the vehicle
    V.start(rate_hz=cfg.DRIVE_LOOP_HZ, 
            max_loop_count=cfg.MAX_LOOPS)
    
    print("You can now go to <your pi ip address>:8887 to drive your car.")


def train(cfg, tub_names, model_name):
    '''
    use the specified data in tub_names to train an artificial neural network
    saves the output trained model as model_name
    '''
    X_keys = ['cam/image_array']
    y_keys = ['user/angle', 'user/throttle']

    def rt(record):
        record['user/angle'] = dk.utils.linear_bin(record['user/angle'])
        return record

    kl = KerasFuzzyAndUltrasonicSensors()
    print('tub_names', tub_names)
    if not tub_names:
        tub_names = os.path.join(cfg.DATA_PATH, '*')
    tubgroup = TubGroup(tub_names)
    train_gen, val_gen = tubgroup.get_train_val_gen(X_keys, y_keys, record_transform=rt,
                                                    batch_size=cfg.BATCH_SIZE,
                                                    train_frac=cfg.TRAIN_TEST_SPLIT)

    model_path = os.path.expanduser(model_name)

    total_records = len(tubgroup.df)
    total_train = int(total_records * cfg.TRAIN_TEST_SPLIT)
    total_val = total_records - total_train
    print('train: %d, validation: %d' % (total_train, total_val))
    steps_per_epoch = total_train // cfg.BATCH_SIZE
    print('steps_per_epoch', steps_per_epoch)

    with tf.device('/gpu:1'): 
        kl.train(train_gen,
             val_gen,
             saved_model_path=model_path,
             steps=steps_per_epoch,
             train_split=cfg.TRAIN_TEST_SPLIT)

if __name__ == '__main__':
    args = docopt(__doc__)
    cfg = dk.load_config()
    
    if args['drive']:
        drive(cfg, model_path = args['--model'], use_joystick=args['--js'], use_fuzzy = False)

    elif args['drive_fuzzy']:
        drive(cfg, model_path = args['--model'], use_joystick=args['--js'], use_fuzzy = True)

    elif args['train']:
        tub = args['--tub']
        model = args['--model']
        cache = not args['--no_cache']
        train(cfg, tub, model)
