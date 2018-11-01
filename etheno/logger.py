import enum
import logging
import os
import tempfile
import threading
import time

import ptyprocess

CRITICAL = logging.CRITICAL
ERROR    = logging.ERROR
WARNING  = logging.WARNING
INFO     = logging.INFO
DEBUG    = logging.DEBUG
NOTSET   = logging.NOTSET

class CGAColors(enum.Enum):
    BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)

ANSI_RESET = "\033[0m"
ANSI_COLOR = "\033[1;%dm"
ANSI_BOLD  = "\033[1m"

LEVEL_COLORS = {
    CRITICAL: CGAColors.MAGENTA,
    ERROR: CGAColors.RED,
    WARNING: CGAColors.YELLOW,
    INFO: CGAColors.GREEN,
    DEBUG: CGAColors.CYAN,
    NOTSET: CGAColors.BLUE
}

def formatter_message(message, use_color = True):
    if use_color:
        message = message.replace("$RESET", RESET_SEQ).replace("$BOLD", BOLD_SEQ)
    else:
        message = message.replace("$RESET", "").replace("$BOLD", "")
    return message

class ComposableFormatter(object):
    def __init__(self, *args, **kwargs):
        if len(args) == 1 and not isinstance(args[0], str):
            self._parent_formatter = args[0]
        else:
            self._parent_formatter = self.new_formatter(*args, **kwargs)
    def new_formatter(self, *args, **kwargs):
        return logging.Formatter(*args, **kwargs)
    def __getattr__(self, name):
        return getattr(self._parent_formatter, name)

class ColorFormatter(ComposableFormatter):
    def reformat(self, fmt):
        for color in CGAColors:
            fmt = fmt.replace("$%s" % color.name, ANSI_COLOR % (30 + color.value))
        fmt = fmt.replace('$RESET', ANSI_RESET)
        fmt = fmt.replace('$BOLD', ANSI_BOLD)
        return fmt
    @staticmethod
    def remove_color(fmt):
        for color in CGAColors:
            fmt = fmt.replace("$%s" % color.name, '')
        fmt = fmt.replace('$RESET', '')
        fmt = fmt.replace('$BOLD', '')
        fmt = fmt.replace('$LEVELCOLOR', '')
        return fmt
    def new_formatter(self, fmt, *args, **kwargs):
        if 'datefmt' in kwargs:
            kwargs['datefmt'] = self.reformat(kwargs['datefmt'])
        return super().new_formatter(self.reformat(fmt), *args, **kwargs)
    def format(self, *args, **kwargs):
        levelcolor = LEVEL_COLORS.get(args[0].levelno, LEVEL_COLORS[NOTSET])
        ret = self._parent_formatter.format(*args, **kwargs)
        ret = ret.replace('$LEVELCOLOR', ANSI_COLOR % (30 + levelcolor.value))
        ret = ret.replace('\n', self.reformat('$RESET $BOLD$BLUE\\$RESET\n'), 1)
        ret = ret.replace('\n', self.reformat('\n$RESET$BOLD$BLUE> $RESET'))
        return ret

class NonInfoFormatter(ComposableFormatter):
    _vanilla_formatter = logging.Formatter()
    def format(self, *args, **kwargs):
        if args and args[0].levelno == INFO:
            return self._vanilla_formatter.format(*args, **kwargs)
        else:
            return self._parent_formatter.format(*args, **kwargs)

class EthenoLogger(object):
    DEFAULT_FORMAT='$RESET$LEVELCOLOR$BOLD%(levelname)-8s $BLUE[$RESET$WHITE%(asctime)14s$BLUE$BOLD]$NAME$RESET %(message)s'
    
    def __init__(self, name, log_level=None, parent=None, cleanup_empty=False):
        self._directory = None
        self.parent = parent
        self.cleanup_empty = cleanup_empty
        self.children = []
        self._descendant_handlers = []
        if log_level is None:
            if parent is None:
                raise ValueError('A logger must be provided a parent if `log_level` is None')
            log_level = parent.log_level
        self._log_level = log_level
        self._logger = logging.getLogger(name)
        self._handlers = [logging.StreamHandler()]
        if log_level is not None:
            self.log_level = log_level
        formatter = ColorFormatter(self.DEFAULT_FORMAT.replace('$NAME', self._name_format()), datefmt='%m$BLUE-$WHITE%d$BLUE|$WHITE%H$BLUE:$WHITE%M$BLUE:$WHITE%S')
        if self.parent is None:
            formatter = NonInfoFormatter(formatter)
        else:
            parent._add_child(self)
        self._handlers[0].setFormatter(formatter)
        self._logger.addHandler(self._handlers[0])
        self._tmpdir = None
        
    def close(self):
        for child in self.children:
            child.close()
        if self.cleanup_empty:
            # first, check any files that handlers have created:
            for h in self._handlers:
                if isinstance(h, logging.FileHandler):
                    if h.stream is not None:
                        log_path = h.stream.name
                        if os.path.exists(log_path) and os.stat(log_path).st_size == 0:
                            h.close()
                            os.remove(log_path)
            # next, check if the output directory can be cleaned up
            if self.directory:
                for dirpath, dirnames, filenames in os.walk(self.directory, topdown=False):
                    if len(dirnames) == 0 and len(filenames) == 0 and dirpath != self.directory:
                        os.rmdir(dirpath)
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

    @property
    def directory(self):
        return self._directory

    def _add_child(self, child):
        if child in self.children:
            raise ValueError("Cannot double-add child logger %s to logger %s" % (child.name, self.name))
        self.children.append(child)
        if self.directory is not None:
            child.save_to_directory(os.path.join(self.directory, child.name))
        else:
            child._tmpdir = tempfile.TemporaryDirectory()
            child.save_to_directory(child._tmpdir)
        parent = self
        while parent is not None:
            for handler in self._descendant_handlers:
                child.addHandler(handler, include_descendants=True)
            parent = parent.parent

    def _name_format(self):
        if self.parent is not None and self.parent.parent is not None:
            ret = self.parent._name_format()
        else:
            ret = ''
        return ret + "[$RESET$WHITE%s$BLUE$BOLD]" % self._logger.name

    def addHandler(self, handler, include_descendants=True, set_log_level=True):
        if set_log_level:
            handler.setLevel(self.log_level)
        self._logger.addHandler(handler)
        self._handlers.append(handler)
        if include_descendants:
            self._descendant_handlers.append(handler)
            for child in self.children:
                if isinstance(child, EthenoLogger):
                    child.addHandler(handler, include_descendants=include_descendants, set_log_level=set_log_level)
                else:
                    child.addHandler(handler)

    def make_logged_file(self, prefix=None, suffix=None, mode='w+b', dir=None):
        '''Returns an opened file stream to a unique file created according to the provided naming scheme'''
        if dir is None:
            dir = ''
        else:
            dir = os.path.relpath(os.path.realpath(dir), start=os.path.realpath(self.directory))
        os.makedirs(os.path.join(self.directory, dir), exist_ok=True)
        i = 1
        while True:
            if i == 1:
                filename = f"{prefix}{suffix}"
            else:
                filename = f"{prefix}{i}{suffix}"
            path = os.path.join(self.directory, dir, filename)
            if not os.path.exists(path):
                return open(path, mode)
            i += 1

    def make_constant_logged_file(self, contents, *args, **kwargs):
        '''Creates a logged file, populates it with the provided contents, and returns the absolute path to the file.'''
        if isinstance(contents, str):
            contents = contents.encode('utf-8')
        with self.make_logged_file(*args, **kwargs) as f:
            f.write(contents)
            return os.path.realpath(f.name)

    def to_log_path(self, absolute_path):
        if self.directory is None:
            return absolute_path
        absolute_path = os.path.realpath(absolute_path)
        dirpath = os.path.realpath(self.directory)
        return os.path.relpath(absolute_path, start=dirpath)

    def save_to_file(self, path, include_descendants=True, log_level=None):
        if log_level is None:
            log_level = self.log_level
        handler = logging.FileHandler(path)
        handler.setLevel(log_level)
        handler.setFormatter(logging.Formatter(ColorFormatter.remove_color(self.DEFAULT_FORMAT.replace('$NAME', self._name_format())), datefmt='%m-%d|%H:%M:%S'))
        self.addHandler(handler, include_descendants=include_descendants, set_log_level=False)

    def save_to_directory(self, path):
        if self.directory == path:
            # we are already set to save to this directory
            return
        elif self.directory is not None:
            raise ValueError("Logger %s's save directory is already set to %s" % (self.name, path))
        self._directory = os.path.realpath(path)
        os.makedirs(path, exist_ok=True)
        self.save_to_file(os.path.join(path, "%s.log" % self.name), include_descendants=False, log_level=DEBUG)
        for child in self.children:
            child.save_to_directory(os.path.join(path, child.name))

    @property
    def log_level(self):
        if self._log_level is None:
            if self.parent is None:
                raise ValueError('A logger must be provided a parent if `log_level` is None')
            return self.parent.log_level
        else:
            return self._log_level

    @log_level.setter
    def log_level(self, level):
        if not isinstance(level, int):
            try:
                level = getattr(logging, str(level).upper())
            except AttributeError:
                raise ValueError("Invalid log level: %s" % level)
        elif level not in (CRITICAL, ERROR, WARNING, INFO, DEBUG):
            raise ValueError("Invalid log level: %d" % level)
        self._log_level = level
        self._logger.setLevel(level)
        for handler in self._handlers:
            handler.setLevel(level)

    def __getattr__(self, name):
        return getattr(self._logger, name)

class StreamLogger(threading.Thread):
    def __init__(self, logger, *streams, newline_char=b'\n'):
        super().__init__(daemon=True)
        self.logger = logger
        self.streams = streams
        if isinstance(newline_char, str):
            newline_char = newline_char.encode('utf-8')
        self._newline_char = newline_char
        self._buffers = [b'' for i in range(len(streams))]
        self._done = False
        self.log = lambda logger, message : logger.info(message)
    def is_done(self):
        return self._done
    def run(self):
        while not self.is_done():
            while True:
                got_byte = False
                try:
                    for i, stream in enumerate(self.streams):
                        byte = stream.read(1)
                        while byte is not None and len(byte):
                            if isinstance(byte, str):
                                byte = byte.encode('utf-8')
                            if byte == self._newline_char:
                                self.log(self.logger, self._buffers[i].decode())
                                self._buffers[i] = b''
                            else:
                                self._buffers[i] += byte
                            got_byte = True
                            byte = stream.read(1)
                except Exception:
                    self._done = True
                if not got_byte or self._done:
                    break
            time.sleep(0.5)

class ProcessLogger(StreamLogger):
    def __init__(self, logger, process):
        self.process = process
        super().__init__(logger, open(process.stdout.fileno(), buffering=1), open(process.stderr.fileno(), buffering=1))
    def is_done(self):
        return self.process.poll() is not None

class PtyLogger(StreamLogger):
    def __init__(self, logger, args, cwd=None, **kwargs):
        self.process = ptyprocess.PtyProcessUnicode.spawn(args, cwd=cwd)
        super().__init__(logger, self.process, **kwargs)
    def is_done(self):
        return not self.process.isalive()
    def __getattr__(self, name):
        return getattr(self.process, name)
    
if __name__ == '__main__':
    logger = EthenoLogger('Testing', DEBUG)
    logger.info('Info')
    logger.critical('Critical')
