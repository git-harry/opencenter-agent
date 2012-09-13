#!/usr/bin/env python

import fcntl
import getopt
import json
import logging
import os
import signal
import socket
import sys
import time
import traceback

from ConfigParser import ConfigParser
from logging.handlers import SysLogHandler

from roushagent.modules import OutputManager
from roushagent.modules import InputManager

class RoushAgent():
    def __init__(self, argv, config_section='main'):
        self.base = os.path.realpath(os.path.join(os.path.dirname(__file__), '..'))
        log = self.log = logging.getLogger()
        self.config_section = config_section
        self.input_handler = None
        self.config = {'main': {}}

        signal.signal(signal.SIGTERM, lambda a, b: self._cleanup())

        try:
            self._setup_scaffolding(argv)
            self._setup_handlers()
        except KeyboardInterrupt:
            self._cleanup()
        except SystemExit:
            raise
        except:
            self._exit()

    def _exit(self):
        log = self.log

        exc_info = sys.exc_info()
        if hasattr(exc_info[0], '__name__'):
            exc_class, exc, tb = exc_info
            tb_path, tb_lineno, tb_func = traceback.extract_tb(tb)[-1][:3]
            log.error('%s (%s:%s in %s)', exc_info[1], tb_path,
                      tb_lineno, tb_func)
        else:  # string exception
            log.error(exc_info[0])
            if log.isEnabledFor(logging.DEBUG):
                print ''
                traceback.print_exception(*exc_info)
            sys.exit(1)

    def _cleanup(self):
        input_handler = self.input_handler
        log = self.log

        if input_handler:
            log.debug('Stopping input handler.')
            input_handler.stop()
        log.debug('Bailing')
        sys.exit(0)

    def _parse_opts(self, argv):
        background = debug = False
        configfile = None

        try:
            opts, args = getopt.getopt(argv, 'c:vd')
        except getopt.GetoptError as err:
            print str(err)
            usage()
            sys.exit(1)

        for o, a in opts:
            if o == '-c':
                configfile = a
            elif o == '-v':
                debug = True
            elif o == '-d':
                background = True
            else:
                usage()
                sys.exit(1)

        return background, debug, configfile

    def _read_config(self, configfile, defaults={}):
        cp = ConfigParser(defaults=defaults)
        cp.read(configfile)
        config = self.config = dict([[s, dict(cp.items(s))] for s in cp.sections()])
        config_section = self.config_section

        if config_section in config:
            if 'include' in config[config_section]:
                # import and merge a single file
                if not os.path.isfile(config[config_section]['include']):
                    raise RuntimeError(
                        'file %s: include directive %s is not a file' % (
                            configfile,
                            config[config_section]['include'],))

                config = self.config = self._read_config(config[config_section]['include'])

            if 'include_dir' in config[config_section]:
                # import and merge a whole directory
                if not os.path.isdir(config[config_section]['include_dir']):
                    raise RuntimeError(
                        'file %s: include_dir directive %s is not a dir' % (
                            configfile,
                            config[config_section]['include_dir'],))

                for d in sorted(os.listdir(config[config_section]['include_dir'])):
                    import_file = os.path.join(config[config_section]['include_dir'], d)

                    config = self.config = self._read_config(import_file, config)

        # merge in the read config into the exisiting config
        for section in config:
            if section in defaults:
                defaults[section].update(config[section])
            else:
                defaults[section] = config[section]

        return defaults

    def _setup_scaffolding(self, argv):
        background, debug, configfile = self._parse_opts(argv)
        config_section = self.config_section
        config = self.config
        log = self.log

        if debug:
            log.setLevel(logging.DEBUG)
        else:
            log.setLevel(logging.WARNING)

        if configfile:
            config = self.config = self._read_config(configfile, defaults={'base_dir': self.base})

        if background:
            logdev = config[config_section].get('syslog_dev', '/dev/log')
            log.addHandler(SysLogHandler(address=logdev))

            # daemonize
            if os.fork():
                sys.exit(0)
            else:
                os.setsid()
                os.chdir('/')
                os.umask(0)
                if os.fork():
                    sys.exit(0)

            if 'pidfile' in config[config_section]:
                pidfile = open(config[config_section]['pidfile'], 'a+')
                try:
                    fcntl.flock(pidfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except IOError:
                    log.error('Lock exists on pidfile: already running')
                    pidfile.seek(0)
                    pidfile.truncate()
                    pidfile.write(str(os.getpid()))
                    pidfile.flush()
        else:
            log.addHandler(logging.StreamHandler(sys.stderr))

    def _setup_handlers(self):
        config = self.config
        config_section = self.config_section

        # get directory/path layout
        base_dir = config[config_section].get('base_dir', self.base)

        plugin_dir = config[config_section].get('plugin_dir',
                                                os.path.join(base_dir,
                                                             'roushagent/plugins'))
        sys.path.append(os.path.join(plugin_dir, 'lib'))

        # find input and output handlers to load
        default_out = os.path.join(plugin_dir, 'output/plugin_files.py')
        default_in = os.path.join(plugin_dir, 'input/task_input.py')

        output_handlers = config['main'].get('output_handlers', default_out)
        input_handlers = config['main'].get('input_handlers', default_in)

        self.output_handler = OutputManager(
            [x.strip() for x in output_handlers.split(',')], config)
        self.input_handler = InputManager(
            [x.strip() for x in input_handlers.split(',')], config)

    def dispatch(self, one_shot=False):
        try:
            self._dispatch(one_shot)
        except:
            self._exit()

    def _dispatch(self, one_shot):
        output_handler = self.output_handler
        input_handler = self.input_handler
        log = self.log

        # we'll assume non-blocking.  we should negotiate this
        # with the plugins, I suppose

        while not one_shot:
            result = input_handler.fetch()
            if len(result) == 0:
                time.sleep(5)
            else:
                log.debug('Got input from input handler "%s"' %
                          result['plugin'])
                log.debug('Data: %s' % result['input'])

                result['output'] = {'result_code': 255,
                                    'result_str': 'unknown error',
                                    'result_data': ''}

                try:
                    result['output'] = output_handler.dispatch(result['input'])

                except Exception as e:
                    exc_type, exc_value, exc_traceback = sys.exc_info()
                    full_traceback = repr(
                        traceback.format_exception(
                            exc_type, exc_value, exc_traceback))

                    result['output'] = {'result_code': 254,
                                        'result_str': 'dispatch error',
                                        'result_data': full_traceback}
                    print full_traceback
                    log.warn(full_traceback)

                input_handler.result(result)
