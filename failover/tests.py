"""
django-failover tests
"""

########################################################################

from django.test import TestCase
from django.conf import settings
from django.db import connections
from django.utils.unittest import skipUnless
from services.db import Database
from monitor import ServiceMonitor, logger as monitor_logger
from log import FailoverHandler, ServiceOutageExceptionsFilter
import settings as failover_settings
import datetime
import socket
import logging
import time

try:
    # The provided Memcached service class requires a particular memcached
    # python client.
    import memcache
except ImportError:
    test_memcached = False
else:
    test_memcached = (
        settings.CACHES and "default" in settings.CACHES 
        and settings.CACHES["default"]["BACKEND"].endswith("MemcachedCache"))
if test_memcached:
    from services.cache import Memcached

test_celery = hasattr(settings, "BROKER_HOST")
if test_celery:
    from services.celery import Celery
    
####################################################################

class DBSlave(Database):
    """
    Database slave service class for the tests.
    """
    name = "database slave"
    DB_ALIAS = "slave"
    FAILOVER_DB_ALIAS = "default"
    
####################################################################

class LogCaptureHandler(logging.Handler):
    """Logging handler that stores the records it emits.
    """
    records = []
    
    def emit(self, record):
        self.records.append(record)

####################################################################

class FailoverTestCase(TestCase):

    ####################################################################
    
    def setUp(self):
        """Adds a slave connection to settings.DATABASES, which uses the
        sames settings as the default connection but includes a marker to
        distinguish it. Registers the test service class.
        """
        self.orig_services = ServiceMonitor.services
        ServiceMonitor.services.clear()
        
        self.setUpDBSlave()
        if test_memcached:
            self.setUpMemcached()
        if test_celery:
            self.setUpCelery()
        
        for service_class in ServiceMonitor.services:
            self.patch_ping(service_class)
            # Set ping intervals to 0 (ping every time)
            service_class._orig_monitoring_interval = service_class.MONITORING_RETRY_INTERVAL
            service_class._orig_outage_interval = service_class.OUTAGE_RETRY_INTERVAL
            service_class._orig_error_interval = service_class.ERROR_RETRY_INTERVAL
            service_class.MONITORING_RETRY_INTERVAL = 0
            service_class.OUTAGE_RETRY_INTERVAL = 0
            service_class.ERROR_RETRY_INTERVAL = 0
       
        # Register socket.error as an exception class that should trigger
        # monitoring.
        self.orig_failover_exception_classes = failover_settings.OUTAGE_EXCEPTION_CLASSES
        failover_settings.OUTAGE_EXCEPTION_CLASSES = (socket.error,)
        
        # Set up a logger tied to the FailoverHandler.
        self.logger = logging.getLogger("failover_test")
        self.logger.setLevel(logging.ERROR)
        self.log_handler = FailoverHandler()
        self.log_handler.addFilter(ServiceOutageExceptionsFilter())
        self.logger.addHandler(self.log_handler)
        
    ####################################################################
    
    def setUpDBSlave(self):
        ServiceMonitor.register(DBSlave)
        self._orig_db_settings = settings.DATABASES.copy()
        settings.DATABASES["slave"] = settings.DATABASES["default"].copy()
        settings.DATABASES["slave"]["MARKER"] = "slave"
        DBSlave.reload_settings()
        
    ####################################################################
        
    def setUpCelery(self):
        ServiceMonitor.register(Celery)
        self._orig_celery_always_eager = getattr(settings, "CELERY_ALWAYS_EAGER", False)
        settings.CELERY_ALWAYS_EAGER = False
        
    ####################################################################
    
    def setUpMemcached(self):
        ServiceMonitor.register(Memcached)
        
    ####################################################################
    
    def tearDown(self):
        for service_class in ServiceMonitor.services:
            service_class.ping = service_class._orig_ping
            delattr(service_class, "_orig_ping")
            delattr(service_class, "pings")
            service_class.MONITORING_RETRY_INTERVAL = service_class._orig_monitoring_interval
            service_class.OUTAGE_RETRY_INTERVAL = service_class._orig_outage_interval
            service_class.ERROR_RETRY_INTERVAL = service_class._orig_error_interval
            delattr(service_class, "_orig_monitoring_interval")
            delattr(service_class, "_orig_outage_interval")
            delattr(service_class, "_orig_error_interval")
            
            # Clear the last_ping from each service class so as not to impact the
            # next test.
            service_class.last_ping = None
            
        self.tearDownDBSlave()
        if test_memcached:
            self.tearDownMemcached()
        if test_celery:
            self.tearDownCelery()
            
        ServiceMonitor.services = self.orig_services
        failover_settings.OUTAGE_EXCEPTION_CLASSES = self.orig_failover_exception_classes
        
        self.logger.removeHandler(self.log_handler)
    
    ####################################################################
    
    def tearDownDBSlave(self):
        settings.DATABASES = self._orig_db_settings
     
    ####################################################################
        
    def tearDownCelery(self):
        settings.CELERY_ALWAYS_EAGER = self._orig_celery_always_eager
     
    ####################################################################
    
    def tearDownMemcached(self):
        pass
    
    ####################################################################
    
    def patch_ping(self, service_class):
        """Patches the ping method to store the datetime of each ping.
        """
        orig_ping = service_class.ping
        def patched_ping(*args, **kwargs):
            service_class.pings.append(datetime.datetime.now())
            return orig_ping(*args, **kwargs)
        
        if not hasattr(service_class, '_orig_ping'):
            service_class._orig_ping = service_class.ping
        service_class.pings = []
        service_class.ping = patched_ping
        
    ####################################################################

    def simulate_service_outage(self, service_class):
        """Patches the database service ping method to raise a socket error.
        """
        orig_ping = service_class.ping
        
        def error_ping(*args, **kwargs):
            orig_ping(*args, **kwargs)
            raise socket.error()
        
        if not hasattr(service_class, '_orig_ping'):
            service_class._orig_ping = service_class.ping
        service_class.ping = error_ping
        
    ####################################################################
    
    def simulate_service_recovery(self, service_class):
        """Restores the original ping method of the service class.
        """
        service_class.ping = service_class._orig_ping
    
    ####################################################################
    
    def test_db_failover_and_recovery(self):
        """
        Tests that the slave fails over to the default connection settings
        when the slave goes down. Then tests that the original connection is
        restored once the service comes back up.
        """
        # Assert the presence of the marker in the slave connection
        connection = connections["slave"]
        self.assertEqual(connection.settings_dict["MARKER"], "slave")
        
        # Simulate the outage and run the monitoring
        self.simulate_service_outage(DBSlave)
        ServiceMonitor.monitor()
        
        # Verify the results
        connection = connections["slave"]
        self.assertNotIn("MARKER", connection.settings_dict)
    
        # Simulate recovery
        self.simulate_service_recovery(DBSlave)
        ServiceMonitor.monitor()
        
        # Verify the results
        connection = connections["slave"]
        self.assertIn("MARKER", connection.settings_dict)
        self.assertEqual(connection.settings_dict["MARKER"], "slave")
        
    ####################################################################
    
    @skipUnless(test_celery, "Not using celery")
    def test_celery_failover_and_recovery(self):
        """
        Tests that celery fails over to ALWAYS_EAGER when the broker goes
        down. Then tests that ALWAYS_EAGER is restored to False once the
        service comes back up.
        """
        # Assert the intitial settings
        self.assertFalse(settings.CELERY_ALWAYS_EAGER)
        
        # Simulate the outage and run the monitoring
        self.simulate_service_outage(Celery)
        ServiceMonitor.monitor()
        
        # Verify the results
        self.assertTrue(settings.CELERY_ALWAYS_EAGER)
    
        # Simulate recovery
        self.simulate_service_recovery(Celery)
        ServiceMonitor.monitor()
        
        # Verify the results
        self.assertFalse(settings.CELERY_ALWAYS_EAGER)
        
    ####################################################################
    
    def test_exception_logging_failover(self):
        """
        Tests that the slave fails over to the default connection settings
        when the slave goes down and a suspicious exception triggers
        monitoring.
        """
        # Assert the presence of the marker in the slave connection
        connection = connections["slave"]
        self.assertEqual(connection.settings_dict["MARKER"], "slave")
        
        # Simulate the outage
        self.simulate_service_outage(DBSlave)
        
        # Raise an error that triggers monitoring
        try:
            raise socket.error()
        except Exception, e:
            self.logger.error(e, exc_info=e)
        
        # Verify the results
        connection = connections["slave"]
        self.assertNotIn("MARKER", connection.settings_dict)
        
         # Simulate recovery
        self.simulate_service_recovery(DBSlave)
        ServiceMonitor.monitor()
        
        # Verify the results
        connection = connections["slave"]
        self.assertIn("MARKER", connection.settings_dict)
        self.assertEqual(connection.settings_dict["MARKER"], "slave")
        
    ####################################################################
    
    def test_ignore_exception_logging(self):
        """
        Tests that irrelevant exceptions do not trigger monitoring.
        """
        self.assertEqual(len(DBSlave.pings), 0)
        
        # Raise an error that shouldn't trigger monitoring
        try:
            raise ValueError()
        except Exception, e:
            self.logger.error(e)
        
        # Verify the results
        self.assertEqual(len(DBSlave.pings), 0)
        
    ####################################################################
    
    def test_ping_monitoring_interval(self):
        """
        Tests the ping interval during normal monitoring.
        """
        DBSlave.MONITORING_RETRY_INTERVAL = 1
        for i in range(3):
            ServiceMonitor.monitor()
                
        self.assertEqual(len(DBSlave.pings), 1)
        time.sleep(1)
        ServiceMonitor.monitor()
        self.assertEqual(len(DBSlave.pings), 2)
    
    ####################################################################
    
    def test_ping_outage_interval(self):
        """
        Tests the ping interval during an outage.
        """
        self.simulate_service_outage(DBSlave)
        
        DBSlave.MONITORING_RETRY_INTERVAL = 3
        DBSlave.OUTAGE_RETRY_INTERVAL = 0
        
        for i in range(3):
            ServiceMonitor.monitor()
                
        self.assertEqual(len(DBSlave.pings), 3)
  
        self.simulate_service_recovery(DBSlave)
        ServiceMonitor.monitor()
            
    ####################################################################
    
    def test_ping_error_interval(self):
        """
        Tests the ping interval when an error is passed to the
        ServiceMonitor.
        """
        self.simulate_service_outage(DBSlave)
       
        DBSlave.MONITORING_RETRY_INTERVAL = 3
        DBSlave.OUTAGE_RETRY_INTERVAL = 3
        DBSlave.ERROR_RETRY_INTERVAL= 0
        for i in range(3):
            ServiceMonitor.monitor(exception=socket.error())
            
        self.assertEqual(len(DBSlave.pings), 3)
        self.simulate_service_recovery(DBSlave)
        ServiceMonitor.monitor()
        
    ####################################################################
    
    def test_db_log_outage_and_recovery(self):
        """Tests that the slave outage is logged when the outage is
        discovered, and periodically thereafter, and that the recovery is
        also logged.
        """
        orig_interval = ServiceMonitor.OUTAGE_LOGGING_INTERVAL 
        ServiceMonitor.OUTAGE_LOGGING_INTERVAL = 1
            
        # Tie the failover logger to the LogCaptureHandler so we can see what
        # we're logging.
        handler = LogCaptureHandler()
        monitor_logger.addHandler(handler)
        LogCaptureHandler.records = []
        try:
            # Simulate the outage and run the monitoring
            self.simulate_service_outage(DBSlave)
            
            # This should log the outage, but only once
            for i in range(3):
                ServiceMonitor.monitor()
                
            # Verify the results    
            self.assertEqual(len(LogCaptureHandler.records), 1)
            record = LogCaptureHandler.records[0]
            self.assertEqual(record.levelno, logging.CRITICAL)
            self.assertIn(
                "{0} outage. Failover initiated.".format(DBSlave.name), 
                record.message)
            
            # Sleep long enough to log the outage again.
            time.sleep(1)
            ServiceMonitor.monitor()
            
            # Verify the results.
            self.assertEqual(len(LogCaptureHandler.records), 2)
            record = LogCaptureHandler.records[1]
            self.assertEqual(record.levelno, logging.CRITICAL)
        
            # Simulate recovery
            self.simulate_service_recovery(DBSlave)
            ServiceMonitor.monitor()
            
            # Verify the results
            self.assertEqual(len(LogCaptureHandler.records), 3)
            record = LogCaptureHandler.records[2]
            self.assertEqual(record.levelno, logging.INFO)
            self.assertIn(
                "{0} is back up. Recovery complete.".format(DBSlave.name), 
                record.message)
    
        finally:
            ServiceMonitor.OUTAGE_LOGGING_INTERVAL = orig_interval
            monitor_logger.removeHandler(handler)
            LogCaptureHandler.records = []
            
    ####################################################################
    
    @skipUnless(test_memcached, "Not using python-memcached")
    def test_memcached_log_outage_and_recovery(self):
        """Tests that the memcached outage is logged when the outage is
        discovered, and periodically thereafter, and that the recovery is
        also logged. Memcached doesn't require any failover, so the best way
        to test Memcached is to make sure an outage notification and recovery
        notification are sent.
        """
        orig_interval = ServiceMonitor.OUTAGE_LOGGING_INTERVAL 
        ServiceMonitor.OUTAGE_LOGGING_INTERVAL = 1
            
        # Tie the failover logger to the LogCaptureHandler so we can see what
        # we're logging.
        handler = LogCaptureHandler()
        monitor_logger.addHandler(handler)
        LogCaptureHandler.records = []
        try:
            # Simulate the outage and run the monitoring
            self.simulate_service_outage(Memcached)
            
            # This should log the outage, but only once
            for i in range(3):
                ServiceMonitor.monitor()
                
            # Verify the results    
            self.assertEqual(len(LogCaptureHandler.records), 1)
            record = LogCaptureHandler.records[0]
            self.assertEqual(record.levelno, logging.CRITICAL)
            self.assertIn(
                "{0} outage. Failover initiated.".format(Memcached.name), 
                record.message)
            
            # Sleep long enough to log the outage again.
            time.sleep(1)
            ServiceMonitor.monitor()
            
            # Verify the results.
            self.assertEqual(len(LogCaptureHandler.records), 2)
            record = LogCaptureHandler.records[1]
            self.assertEqual(record.levelno, logging.CRITICAL)
        
            # Simulate recovery
            self.simulate_service_recovery(Memcached)
            ServiceMonitor.monitor()
            
            # Verify the results
            self.assertEqual(len(LogCaptureHandler.records), 3)
            record = LogCaptureHandler.records[2]
            self.assertEqual(record.levelno, logging.INFO)
            self.assertIn(
                "{0} is back up. Recovery complete.".format(Memcached.name), 
                record.message)
    
        finally:
            ServiceMonitor.OUTAGE_LOGGING_INTERVAL = orig_interval
            monitor_logger.removeHandler(handler)
            LogCaptureHandler.records = []
            
####################################################################