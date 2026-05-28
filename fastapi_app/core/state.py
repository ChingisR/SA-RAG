"""
Global state and fault tolerance module.

This module implements thread-safe state containers and fault tolerance patterns,
such as the GPUCircuitBreaker, which protects the application from cascading failures
when remote inference endpoints become temporarily unavailable.
"""

import time
from threading import Lock

class GPUCircuitBreaker:
    """
    A thread-safe Circuit Breaker implementation for remote GPU service calls.
    
    States:
    - CLOSED: Service is healthy. Requests are allowed.
    - OPEN: Service has failed consecutively exceeding the threshold. Requests are blocked.
    - HALF_OPEN: Recovery timeout has elapsed. A single test request is allowed to check if service recovered.
    """
    def __init__(self, failure_threshold=3, recovery_timeout=60):
        """
        Initialize the circuit breaker.
        
        Args:
            failure_threshold (int): Number of consecutive failures before opening the circuit.
            recovery_timeout (int): Seconds to wait in OPEN state before transitioning to HALF_OPEN.
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.last_failure_time = 0
        self.state = "CLOSED"  # CLOSED → OPEN → HALF_OPEN
        self._lock = Lock()

    def can_execute(self) -> bool:
        """
        Determine if a request should be allowed based on the current circuit state.
        
        Returns:
            bool: True if request is allowed, False if circuit is OPEN.
        """
        with self._lock:
            if self.state == "CLOSED": 
                return True
            if self.state == "OPEN":
                # Check if the recovery timeout has elapsed
                if time.time() - self.last_failure_time > self.recovery_timeout:
                    self.state = "HALF_OPEN"
                    return True
                return False
            # If HALF_OPEN, allow execution to test the service
            return True

    def record_success(self):
        """
        Record a successful service call. Resets failures and closes the circuit.
        """
        with self._lock:
            self.failures = 0
            self.state = "CLOSED"

    def record_failure(self):
        """
        Record a failed service call. Increments failure count and opens circuit if threshold is reached.
        """
        with self._lock:
            self.failures += 1
            self.last_failure_time = time.time()
            if self.failures >= self.failure_threshold:
                self.state = "OPEN"

# Global singleton instance to be used across FastAPI worker threads
gpu_breaker = GPUCircuitBreaker(failure_threshold=3, recovery_timeout=60)
