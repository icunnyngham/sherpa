import logging
import numpy
import pymongo
from pymongo import MongoClient
import subprocess
import time
import os
import socket
try:
    from subprocess import DEVNULL # py3k
except ImportError:
    import os
    DEVNULL = open(os.devnull, 'wb')
import sherpa


logging.basicConfig(level=logging.DEBUG)
dblogger = logging.getLogger(__name__)


class Database(object):
    """
    Manages a Mongo-DB for storing metrics and delivering parameters to trials.

    The Mongo-DB contains one database that serves as a queue of future trials
    and one to store results of active and finished trials.

    # Attributes:
        dbpath (str): the path where Mongo-DB stores its files.
        port (int): the port on which the Mongo-DB should run.
    """
    def __init__(self, db_dir, port=27010, reinstantiated=False):
        self.client = MongoClient(port=port)
        self.db = self.client.sherpa
        self.collected_results = []
        self.mongo_process = None
        self.dir = db_dir
        self.port = port
        if reinstantiated:
            self.get_new_results()

    def close(self):
        print('Closing MongoDB!')
        self.mongo_process.terminate()

    def start(self):
        """
        Runs the DB in a sub-process.
        """
        dblogger.debug("Starting MongoDB...\nDIR:\t{}\nADDRESS:\t{}:{}".format(self.dir, socket.gethostname(), self.port))
        cmd = ['mongod',
               '--dbpath', self.dir,
               '--port', str(self.port),
               '--logpath', os.path.join(self.dir, "log.txt")]
        try:
            self.mongo_process = subprocess.Popen(cmd)
        except FileNotFoundError as e:
            raise FileNotFoundError(str(e) + "\nCheck that MongoDB is installed and in PATH.")
        time.sleep(1)
        self.check_db_status()

    def check_db_status(self):
        """
        Checks whether database is still running.
        """
        status = self.mongo_process.poll()
        if status:
            raise EnvironmentError("Database exited with code {}".format(status))

    def get_new_results(self):
        """
        Checks database for new results.

        # Returns:
            (list[dict]) where each dict is one row from the DB.
        """
        self.check_db_status()
        new_results = []
        for entry in self.db.results.find():
            result = entry
            mongo_id = result.pop('_id')
            if mongo_id not in self.collected_results:
                new_results.append(result)
                self.collected_results.append(mongo_id)
        return new_results

    def enqueue_trial(self, trial):
        """
        Puts a new trial in the queue for trial scripts to get.
        """
        self.check_db_status()
        trial = {'trial_id': trial.id,
                 'parameters': trial.parameters}
        try:
            t_id = self.db.trials.insert_one(trial).inserted_id
        except pymongo.errors.InvalidDocument:
            new_params = {}
            for k, v in trial['parameters'].items():
                if isinstance(v, numpy.int64):
                    v = int(v)
                new_params[k] = v
            trial['parameters'] = new_params
            t_id = self.db.trials.insert_one(trial).inserted_id

    def add_for_stopping(self, trial_id):
        """
        Adds a trial for stopping.

        In the trial-script this will raise an exception causing the trial to
        stop.

        # Arguments:
            trial_id (int): the ID of the trial to stop.
        """
        self.check_db_status()
        # dblogger.debug("Adding {} to DB".format({'trial_id': trial_id}))
        self.db.stop.insert_one({'trial_id': trial_id}).inserted_id

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class Client(object):
    """
    Registers a session with a Sherpa Study via the port of the database.

    This function is called from trial-scripts only.

    # Attributes:
        host (str): the host that runs the database. Passed host, host set via
            environment variable or 'localhost' in that order.
        port (int): port that database is running on. Passed port, port set via
            environment variable or 27010 in that order.
    """
    def __init__(self, host=None, port=None, **mongo_client_args):
        """
        # Arguments:
        host (str): the host that runs the database. Generally not needed since
            the scheduler passes the DB-host as an environment variable.
        port (int): port that database is running on. Generally not needed since
            the scheduler passes the DB-port as an environment variable.
        """
        host = host or os.environ.get('SHERPA_DB_HOST') or 'localhost'
        port = port or os.environ.get('SHERPA_DB_PORT') or 27010
        self.client = MongoClient(host, int(port), **mongo_client_args)
        self.db = self.client.sherpa

    def get_trial(self, id=None):
        """
        Returns the next trial from a Sherpa Study.

        # Arguments:
            client (sherpa.SherpaClient): the client obtained from registering with
                a study.

        # Returns:
            (sherpa.Trial)
        """
        assert id or os.environ.get('SHERPA_TRIAL_ID'), "Environment-variable SHERPA_TRIAL_ID not found. Scheduler needs to set this variable in the environment when submitting a job"
        trial_id = int(id or os.environ.get('SHERPA_TRIAL_ID'))
        for _ in range(5):
            g = (entry for entry in self.db.trials.find({'trial_id': trial_id}))
            t = next(g)
            if t:
                break
            time.sleep(10)
        if not t:
            raise RuntimeError("No Trial Found!")
        return sherpa.Trial(id=t.get('trial_id'), parameters=t.get('parameters'))

    def send_metrics(self, trial, iteration, objective, context={}):
        """
        Sends metrics for a trial to database.

        # Arguments:
            client (sherpa.SherpaClient): client to the database.
            trial (sherpa.Trial): trial to send metrics for.
            iteration (int): the iteration e.g. epoch the metrics are for.
            objective (float): the objective value.
            context (dict): other metric-values.
        """
        result = {'parameters': trial.parameters,
                  'trial_id': trial.id,
                  'objective': objective,
                  'iteration': iteration,
                  'context': context}
        self.db.results.insert_one(result)

        for entry in self.db.stop.find():
            if entry.get('trial_id') == trial.id:
                raise StopIteration("Trial listed for stopping.")