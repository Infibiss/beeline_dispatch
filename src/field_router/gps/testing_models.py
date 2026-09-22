"""Deliberate opt-in failure models for isolation checks; never default."""
import os
import time


class FailingDemoModel:
    def analyze(self, job):
        raise RuntimeError('Intentional GPS model failure')


class HangingDemoModel:
    def analyze(self, job):
        time.sleep(60)


class CrashingDemoModel:
    def analyze(self, job):
        os._exit(17)
