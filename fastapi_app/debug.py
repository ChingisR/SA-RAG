import faulthandler
import threading
import sys
faulthandler.enable()
threading.Timer(5, faulthandler.dump_traceback, kwargs=dict(file=sys.stderr, all_threads=True)).start()
import main
