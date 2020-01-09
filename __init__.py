#!/usr/bin/env python2
# coding: utf-8
import os
from sys import argv
if '-t' in argv:
    TESTING = True
    argv.remove('-t')
else:
    TESTING = False
if not TESTING:
    os.environ['KIVY_WINDOW'] = 'sdl2'
    os.environ['KIVY_GL_BACKEND'] = 'gl'
from kivy import kivy_data_dir
from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.config import Config
from kivy.base import ExceptionHandler, ExceptionManager
from kivy.properties import OptionProperty, BooleanProperty, DictProperty, NumericProperty
from os.path import join, abspath, expanduser, basename, splitext
from datetime import datetime, timedelta
from subprocess import Popen
import site
import logging
import threading
from elements import UltraKeyboard
from home import *
from files import *
from status import *
from settings import *
import parameters as p


# inherit from threading.thread => inherits start() method to run() in new thread
class mainApp(App, threading.Thread): #Handles Communication with Klipper

    # Property for controlling the state as shown in the statusbar.
    state = OptionProperty("initializing", options=[
        # Every string set has to be in this list
        "initializing",
        "ready",
        "error",
        "error disconnected"
        ])
    print_state = OptionProperty("no_printjob", options=[
        "no_printjob",
        "printing",
        "paused",
        "stopped",
        "done",
        ])
    homed = DictProperty({}) #updated by handle_homed event handler
    temp = DictProperty({}) #{'B':[setpoint, current], 'T0': ...} updated by scheduled update_home -> get_temp
    printer_objects_available = BooleanProperty(False) #updated with handle_connect
    queued_files = ListProperty()
    print_time = StringProperty() #updated by handle_print_time_calc
    print_done_time = StringProperty()
    progress = NumericProperty(0) #updated by scheduled update_home
    pos = ListProperty([0,0,0]) #updated by scheduled update_home
    #tuning  #updated by upate_printing
    speed = NumericProperty(100)
    flow = NumericProperty(100)
    fan_speed = NumericProperty(0)
    z_adjust = NumericProperty(0)
    acceleration = NumericProperty(2000)
    pressure_advance = NumericProperty(0)
    #config
    default_pressure_advance = NumericProperty(0)
    default_acceleration = NumericProperty(2000)

    def __init__(self, config = None, **kwargs):
        logging.info("Kivy app initializing...")
        self.temp = {'T0':(0,0), 'T1':(0,0), 'B':(0,0)}
        self.homed = {'x':False, 'y':False, 'z':False}
        self.scheduled_updating = None
        if not TESTING:
            self.kgui_config = config
            self.printer = config.get_printer()
            self.reactor = self.printer.get_reactor()
            self.klipper_config_manager = self.printer.objects['configfile']
            self.klipper_config = self.klipper_config_manager.read_main_config()
            #read config
            self.invert_z_controls = self.kgui_config.getboolean('invert_z_controls', False)
            self.xy_homing_controls = self.kgui_config.getboolean('xy_homing_controls', True)
            stepper_config = {'x': self.klipper_config.getsection('stepper_x'),
                              'y': self.klipper_config.getsection('stepper_y'),
                              'z': self.klipper_config.getsection('stepper_z')}
            self.pos_max = {i:stepper_config[i].getfloat('position_max', 200) for i in ('x','y','z')}
            self.pos_min = {i:stepper_config[i].getfloat('position_min', 0) for i in ('x','y','z')}#maybe use position_min, position_max = rail.get_range()
            self.filament_diameter = self.klipper_config.getsection("extruder").getfloat("filament_diameter", 1.75)
            #check whether the right sdcard path is configured
            configured_sdpath = expanduser(self.klipper_config.getsection("virtual_sdcard").get("path", None))
            if abspath(configured_sdpath) != abspath(p.sdcard_path):
                logging.warning("virtual_sdcard path misconfigured: is {} and not {}".format(configured_sdpath, p.sdcard_path))
            #count how many extruders exist before drawing homescreen
            for i in range(1, 10):
                try: klipper_config.getsection('extruder{i}'.format(**locals()))
                except: self.extruder_count = i; break
            #register event handlers
            self.printer.register_event_handler("klippy:connect", self.handle_connect) #printer_objects are available
            self.printer.register_event_handler("klippy:ready", self.handle_ready) #connect handlers have run
            self.printer.register_event_handler("klippy:disconnect", self.handle_disconnect)
            self.printer.register_event_handler("klippy:shutdown", self.handle_shutdown)
            self.printer.register_event_handler("klippy:exception", self.handle_exception)
            self.printer.register_event_handler("homing:homed_rails", self.handle_homed)
        else:
            self.pos_max = {'x':200, 'y':0}
            self.pos_min = {'x':0, 'y':0}
            self.filament_diameter = 1.75
            self.extruders = [None, None, None]
        super(mainApp, self).__init__(**kwargs)

    def handle_connect(self): #runs in klippy thread
        self.fan = self.printer.lookup_object('fan', None)
        self.gcode = self.printer.lookup_object('gcode')
        self.sdcard = self.printer.lookup_object('virtual_sdcard', None)
        self.toolhead = self.printer.lookup_object('toolhead')
        self.bed_mesh = self.printer.lookup_object('bed_mesh', None)
        self.heater_manager = self.printer.lookup_object('heater', None)
        self.heaters = {}
        if 'heater_bed' in self.heater_manager.heaters: 
            self.heaters['B'] = self.heater_manager.heaters['heater_bed']
        for i in range(self.extruder_count):
            self.heaters['T{}'.format(i)] = self.heater_manager.heaters['extruder{}'.format('' if i==0 else i)]
        self.extruders = []
        for i in range(self.extruder_count):
            self.extruders.append(self.printer.lookup_object('extruder{}'.format('' if i==0 else i)))

        self.printer_objects_available = True
        Clock.schedule_once(self.bind_updating, 0)
        Clock.schedule_once(self.control_updating, 0)
        Clock.schedule_interval(self.update_always, 0.3)

    def handle_ready(self):
        self.state = "ready"

    def handle_shutdown(self): # is called when system shuts down
        logging.info("handled shutdown ")
        self.stop()

    def handle_exception(self, message):
        self.state = "error"
        ErrorPopup(message = message).open()

    def handle_disconnect(self):
        self.state = "error disconnected"
        logging.info("run handle_disconnect -> shutdown gui")
        self.stop()

    def handle_homed(self, homing, rails):
        for rail in rails:
            self.homed[rail.steppers[0].get_name(short=True)] = True

### KLIPPER THREAD ^
########################################################################################
### KGUI    THREAD v
        
    def run(self):
        logging.info("Kivy app.run")
        Clock.schedule_once(self.setup_after_run, 1)
        super(mainApp, self).run()

    def setup_after_run(self, dt):
        self.notify = Notifications()
        try:
            self.root_window.set_vkeyboard_class(UltraKeyboard)
        except:
            logging.warning("root_window wasnt available")

    def bind_updating(self, *args):
        self.root.ids.tabs.bind(current_tab=self.control_updating)
        self.root.ids.tabs.ids.home_tab.ids.hs_manager.bind(current=self.control_updating)

    def control_updating(self, *args):
        tab = self.root.ids.tabs.current_tab
        if self.scheduled_updating is not None:
            Clock.unschedule(self.scheduled_updating)
        if tab == self.root.ids.tabs.ids.home_tab:
            if tab.ids.hs_manager.current == "homescreen":
                self.update_home()
                self.scheduled_updating = Clock.schedule_interval(self.update_home, 1)
            if tab.ids.hs_manager.current == "printingscreen":
                self.update_printing()
                self.scheduled_updating = Clock.schedule_interval(self.update_printing, 1.5)
        if tab == self.root.ids.tabs.ids.set_tab:
            self.update_setting()
            self.scheduled_updating = Clock.schedule_interval(self.update_setting, 2)

    def update_always(self, *args):
        self.print_state = self.sdcard.get_status(self.reactor.monotonic())['state']

    def update_home(self, *args):
        self.get_printjob_state()
        self.get_homing_state()
        self.get_temp()
        self.get_pos()

    def update_printing(self, *args):
        self.get_pressure_advance()
        self.get_printjob_state()
        self.get_acceleration()
        self.get_z_adjust()
        self.get_speed()
        self.get_flow()
        self.get_temp()
        self.get_fan()

    def update_setting(self, *args):
        self.get_config('extruder', 'pressure_advance', 'default_pressure_advance')
        self.get_config('printer', 'max_accel', 'default_acceleration', 'int')

    def get_printjob_state(self, *args):

        def format_time(seconds):
            minutes = int((seconds % 3600) // 60)
            hours  =  int(seconds // 3600)
            if hours:
                return "{hours} hours, {minutes} minutes".format(**locals())
            else:
                return "{minutes} minutes".format(**locals())

        s = self.sdcard.get_status(self.reactor.monotonic())
        if len(s['queued_files']) > max(len(self.queued_files), 1): # added file to q
            self.notify.show("Added Printjob", "Added {basename(s['queued_files'][0])} to print Queue".format(**locals()))

        self.queued_files = s['queued_files']

        if s['state'] != self.print_state: # state change
            self.print_state = s['state']
            if s['state'] == 'printing':
                self.notify.show("Started printing", "Started printing {}".format(basename(s['queued_files'][0])), delay=4)
            elif s['state'] == 'done':
                self.progress = 1
                self.print_time = "done" 
                self.print_done_time = "took " + format_time(self.sdcard.get_printed_time(self.reactor.monotonic()))
            elif s['state'] == 'stopped':
                self.print_time = "stopped"
                self.print_done_time = ""

        if s['state'] == 'printing'\
        or s['state'] == 'paused': # we need a print time prediction
            if s['progress'] is None: # no prediction could be made yet
                self.progress = 0
                self.print_time = ""
                self.print_done_time = ""
            else:
                remaining = timedelta(seconds = s['estimated_remaining_time'])
                done = datetime.now() + remaining
                tomorrow = datetime.now() + timedelta(days=1)
                self.progress = s['progress']
                self.print_time = format_time(remaining.total_seconds()) + " remaining"
                if done.day == datetime.now().day:
                    self.print_done_time = done.strftime("ca. %-H:%M")
                elif done.day == tomorrow.day:
                    self.print_done_time = done.strftime("ca. tomorrow %-H:%M")
                else:
                    self.print_done_time = done.strftime("ca. %a %-H:%M")
            
##################################################################
### TUNING

    def get_z_adjust(self):
        self.z_adjust = self.gcode.homing_position[2]
    def send_z_adjust(self, offset):
        self.z_adjust = offset
        def set_z_offset(e):  
            #keeps the difference between base_position and homing_position the same
            #works like this: if base_position = 5, put origin of gcode coordinate system at + 5, 
            #used for z tuning or G92 zeroing
            delta = offset - self.gcode.homing_position[2] #difference between old and new offset
            self.gcode.base_position[2] += delta
            self.gcode.homing_position[2] = offset
            #Move to offset
            self.gcode.last_position[2] += delta
            self.gcode.move_with_transform(self.gcode.last_position, 5) #sets speed for adjustment move
        self.reactor.register_async_callback(set_z_offset)

    def get_speed(self):
        self.speed = self.gcode.speed_factor*60*100 #speed factor also converts from mm/sec to mm/min
    def send_speed(self, val):
        self.speed = val
        val = val/(60.*100.)
        def set_speed(e):
            self.gcode.speed = self.gcode._get_gcode_speed() * val
            self.gcode.speed_factor = val
        self.reactor.register_async_callback(set_speed)

    def get_flow(self):
        self.flow = self.gcode.extrude_factor*100
    def send_flow(self, val):
        self.flow = val
        def set_flow(e):
            new_extrude_factor = val/100.
            last_e_pos = self.gcode.last_position[3]
            e_value = (last_e_pos - self.gcode.base_position[3]) / self.gcode.extrude_factor
            self.gcode.base_position[3] = last_e_pos - e_value * new_extrude_factor
            self.gcode.extrude_factor = new_extrude_factor
        self.reactor.register_async_callback(set_flow)

    def get_fan(self):
        self.fan_speed = self.fan.last_fan_value * 100 / self.fan.max_power
    def send_fan(self, speed):
        self.fan_speed = speed
        self.reactor.register_async_callback(lambda e: self.fan.set_speed(self.toolhead.get_last_move_time(), speed/100.))
    
    def get_pressure_advance(self):#gives pressure_advance value of 1. extruder for now
        self.pressure_advance = self.extruders[0].get_status(self.reactor.monotonic())['pressure_advance']
    def send_pressure_advance(self, val):
        self.pressure_advance = val
        def set_pressure_advance(e):
            for extruder in self.extruders:
                extruder._set_pressure_advance(val, extruder.pressure_advance_smooth_time)
        self.reactor.register_async_callback(set_pressure_advance)

    def get_acceleration(self):
        self.acceleration = self.toolhead.max_accel
    def send_acceleration(self, val):
        self.acceleration = val
        self.toolhead.max_accel = val
        self.reactor.register_async_callback(lambda e: self.toolhead._calc_junction_deviation())

### TUNING
#####################################################################

    def get_config(self, section, option, property_name, ty=None):
        logging.info("wrote {} from section {} to {}".format(option, section, property_name))
        if TESTING:
            setattr(self, property_name, 77)
            return
        def read_config(e):
            Section = self.klipper_config.getsection(section)
            if ty == 'int':
                val = Section.getint(option)
            else:
                val = Section.get(option)
            setattr(self, property_name, val)
        self.reactor.register_async_callback(read_config)
    
    def set_config(self, section, option, value):
        logging.info("trying to set config section {} option {} to value {}".format(section, option, value))
        self.reactor.register_async_callback(lambda e: self.klipper_config_manager.set(section, option, value))

    def write_config(self, section, option, value):
        logging.info( 'trying to write section: {} option: {}, value: {} to config'.format(section, option, value))
        def write_conf(e):
            self.klipper_config_manager.set(section, option, value)
            self.klipper_config_manager.cmd_SAVE_CONFIG(None)
        self.reactor.register_async_callback(write_conf)

    def write_pressure_advance(self, val):
        for i in range(self.extruder_count):
            self.write_config('extruder{}'.format(i if i != '0' else ''), 'pressure_advance', val)

    def get_temp(self, dt=None):
        # schedule reading temp in klipper thread which schedules displaying the read value in kgui thread
        def read_temp(e):
            if self.heater_manager is not None:
                t = {}
                for heater_id, sensor in self.heater_manager.get_gcode_sensors():
                    current, target = sensor.get_temp(self.reactor.monotonic()) #get temp at current point in time
                    self.temp[heater_id] = (target, current)
        self.reactor.register_async_callback(read_temp)

    def send_temp(self, temp, heater_id):
        self.reactor.register_async_callback((lambda e: self.heaters[heater_id].set_temp(self.toolhead.get_last_move_time(), temp)))

    def get_homing_state(self):
        homed_axes_string = self.toolhead.get_status(self.reactor.monotonic())['homed_axes']
        for axis in self.homed.keys():
            self.homed[axis] = axis in homed_axes_string

    def send_home(self, axis):
        self.reactor.register_async_callback((lambda e: self.gcode.cmd_G28(axis.upper())))

    def send_motors_off(self):
        self.reactor.register_async_callback(lambda e: self.gcode.run_script_from_command("M18"))

    def get_pos(self):
        def read_pos(e):
            pos = self.toolhead.get_position()
            self.pos = pos
        self.reactor.register_async_callback(read_pos)

    def send_pos(self, x=None, y=None, z=None, e=None, speed=15):  
        def set_pos(e):
            pos = self.toolhead.get_position()
            new_pos = [x,y,z,e]
            homed_axes = self.toolhead.get_status(self.reactor.monotonic())['homed_axes']
            new_pos = [new_pos[i] if p in homed_axes else None for i, p in enumerate('xyze')] #check whether axes are still homed
            pos = [p if p is not None else pos[i] for i, p in enumerate(new_pos)] #replace coordinates not given with current pos
            self.toolhead.drip_move(pos, speed)
        self.reactor.register_async_callback(set_pos)

    def send_z_up(self):
        self.send_pos(z=self.pos_min['z'])
    def send_z_down(self):
        self.send_pos(z=self.pos_max['z'])
    def send_z_stop(self):
        self.reactor.register_async_callback((lambda e: self.toolhead.signal_drip_mode_end()))

    def send_calibrate(self):
        self.reactor.register_async_callback((lambda e: self.bed_mesh.calibrate.cmd_BED_MESH_CALIBRATE(0)))

    def send_start(self, filepath=None):
        if filepath is None:
            def start_reprint(e):
                if self.sdcard.current_file: # reprint the last file
                    filepath = self.sdcard.current_file.name
                else: return
                self.sdcard.add_printjob(filepath)
                Clock.schedule_once(self.get_printjob_state, 0)
            self.reactor.register_async_callback(start_reprint)
        else:
            def start_print(e):
                self.sdcard.add_printjob(filepath)
                Clock.schedule_once(self.get_printjob_state, 0)
            self.reactor.register_async_callback(start_print)

    def send_resume(self):
        def resume_print(e):
            self.sdcard.resume_printjob()
            Clock.schedule_once(self.get_printjob_state, 0)
        self.reactor.register_async_callback(resume_print)

    def send_stop(self):
        def stop_print(e):
            self.sdcard.stop_printjob()
            Clock.schedule_once(self.get_printjob_state, 0)
        self.reactor.register_async_callback(stop_print)

    def send_pause(self):
        def pause_print(e):
            self.sdcard.pause_printjob()
            Clock.schedule_once(self.get_printjob_state, 0)
        self.reactor.register_async_callback(pause_print)

    def poweroff(self):
        Popen(['sudo','systemctl', 'poweroff'])
    def reboot(self):
        Popen(['sudo','systemctl', 'reboot'])
    def restart_klipper(self):
        """Quit and restart klipper and GUI"""
        logging.info("attempting a firmware restart")
        self.reactor.register_async_callback(self.gcode.cmd_FIRMWARE_RESTART, 10)
        self.stop()
    def quit(self):
        """Stop klipper and GUI, returns to tty"""
        Popen(['sudo', 'systemctl', 'stop', 'klipper.service'])

########################################################################################
### KLIPPER THREAD v

class PopupExceptionHandler(ExceptionHandler):
    def handle_exception(self, exception):
        App.get_running_app().handle_exception(repr(exception))
        logging.exception(exception)
        return ExceptionManager.PASS

def set_kivy_config():
    # This needs an absolute path otherwise config will only be loaded when
    # working directory is the parent directory
    if TESTING:
        Config.read(join(p.kgui_dir, "config-test.ini"))
    else:
        Config.read(join(p.kgui_dir, "config.ini"))
        # Read the display rotation value
        try:
            with open("/boot/config.txt", "r") as file_:
                lines = file_.read().splitlines()
        except IOError:
            # Assume 90 degree rotation (config default) in case
            # /boot/config.txt isn't found
            rotation = 1
        else:
            rotation_string = [i for i in lines if i.startswith("display_hdmi_rotate")][0]
            # The number should always be at index 20
            rotation = int(rotation_string[20])
        # rotation should only be 1 for 90deg or 3 for 270deg
        # Set the input config option to rotate the touchinput explicitly for kivy
        if rotation == 3:
            Config.set("input", "device_%(name)s",
                "probesysfs,provider=mtdev,param=rotation=270,param=invert_y=1")

    #load a custom style.kv with changes to filechooser and more
    Builder.unload_file(join(kivy_data_dir, "style.kv"))
    Builder.load_file(join(p.kgui_dir, "style.kv"))

# Catch KGUI exceptions and display popup
ExceptionManager.add_handler(PopupExceptionHandler())
# Add parent directory to sys.path so main.kv (parser.py) can import from it
site.addsitedir(p.kgui_dir)
# Read custom Kivy config file, set rotation and load custom style.kv
set_kivy_config()

#Entry point, order of execution: __init__()  run()  main.kv  setup_after_run()  handle_connect()  handle_ready()
def load_config(config):
    kgui_object = mainApp(config)
    kgui_object.start()
    return kgui_object

if __name__ == "__main__":
    mainApp().run()