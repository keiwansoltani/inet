#!/usr/bin/env python3
#
# Fingerprint-based regression tested for the INET Framework.
#
# Accepts one or more CSV files with 4 columns: working directory,
# options to opp_run, simulation time limit, expected fingerprint.
# The program runs the INET simulations in the CSV files, and
# reports fingerprint mismatches as FAILed test cases. To facilitate
# test suite maintenance, the program also creates a new file (or files)
# with the updated fingerprints.
#
# Implementation is based on Python's unit testing library, so it can be
# integrated into larger test suites with minimal effort
#
# Author: Andras Varga
#

import argparse
import copy
import csv
import glob
import os
import re
import subprocess
import sys
import time
import random
import unittest
from io import StringIO
from distutils import spawn


import redis, rq, fingerprints_worker

localInetRoot = os.path.abspath("../..")
remoteInetRoot = "/opt/inet"

sep = ";" if sys.platform == 'win32' else ':'
nedPath = remoteInetRoot + "/src" + sep + remoteInetRoot + "/examples" + sep + remoteInetRoot + "/showcases" + sep + remoteInetRoot + "/tutorials" + sep + remoteInetRoot + "/tests/networks"
inetLib = remoteInetRoot + "/src/INET"
cpuTimeLimit = "30000s"
logFile = "test.out"
extraOppRunArgs = ""
exitCode = 0


if spawn.find_executable("opp_run_dbg"):
    executable = "opp_run_dbg"
elif spawn.find_executable("opp_run"):
    executable = "opp_run"
elif spawn.find_executable("opp_run_release"):
    executable = "opp_run_release"

class FingerprintTestCaseGenerator():
    fileToSimulationsMap = {}
    def generateFromCSV(self, csvFileList, filterRegexList, excludeFilterRegexList, repeat):
        testcases = []
        for csvFile in csvFileList:
            simulations = self.parseSimulationsTable(csvFile)
            self.fileToSimulationsMap[csvFile] = simulations
            testcases.extend(self.generateFromDictList(simulations, filterRegexList, excludeFilterRegexList, repeat))
        return testcases

    def generateFromDictList(self, simulations, filterRegexList, excludeFilterRegexList, repeat):
        class StoreFingerprintCallback:
            def __init__(self, simulation):
                self.simulation = simulation
            def __call__(self, fingerprint):
                self.simulation['computedFingerprint'] = fingerprint

        class StoreExitcodeCallback:
            def __init__(self, simulation):
                self.simulation = simulation
            def __call__(self, exitcode):
                self.simulation['exitcode'] = exitcode

        testcases = []
        for simulation in simulations:
            title = simulation['wd'] + " " + simulation['args']
            if not filterRegexList or ['x' for regex in filterRegexList if re.search(regex, title)]: # if any regex matches title
                if not excludeFilterRegexList or not ['x' for regex in excludeFilterRegexList if re.search(regex, title)]: # if NO exclude-regex matches title
                    testcases.append(FingerprintTestCase(title, simulation['file'], simulation['wd'], simulation['args'],
                            simulation['simtimelimit'], simulation['fingerprint'], StoreFingerprintCallback(simulation), StoreExitcodeCallback(simulation), repeat))
        return testcases

    def commentRemover(self, csvData):
        p = re.compile(' *#.*$')
        for line in csvData:
            yield p.sub('',line.decode("utf-8"))

    # parse the CSV into a list of dicts
    def parseSimulationsTable(self, csvFile):
        simulations = []
        f = open(csvFile, 'rb')
        csvReader = csv.reader(self.commentRemover(f), delimiter=',', quotechar='"', skipinitialspace=True)
        for fields in csvReader:
            if len(fields) == 0:
                continue        # empty line
            if len(fields) != 4:
                raise Exception("Line " + str(csvReader.line_num) + " must contain 4 items, but contains " + str(len(fields)) + ": " + '"' + '", "'.join(fields) + '"')
            simulations.append({'file': csvFile, 'line' : csvReader.line_num, 'wd': fields[0], 'args': fields[1], 'simtimelimit': fields[2], 'fingerprint': fields[3]})
        f.close()
        return simulations

    def writeUpdatedCSVFiles(self):
        for csvFile, simulations in self.fileToSimulationsMap.items():
            updatedContents = self.formatUpdatedSimulationsTable(csvFile, simulations)
            if updatedContents:
                updatedFile = csvFile + ".UPDATED"
                ff = open(updatedFile, 'w')
                ff.write(updatedContents)
                ff.close()
                print("Check " + updatedFile + " for updated fingerprints")

    def writeFailedCSVFiles(self):
        for csvFile, simulations in self.fileToSimulationsMap.items():
            failedContents = self.formatFailedSimulationsTable(csvFile, simulations)
            if failedContents:
                failedFile = csvFile + ".FAILED"
                ff = open(failedFile, 'w')
                ff.write(failedContents)
                ff.close()
                print("Check " + failedFile + " for failed fingerprints")

    def writeErrorCSVFiles(self):
        for csvFile, simulations in self.fileToSimulationsMap.items():
            errorContents = self.formatErrorSimulationsTable(csvFile, simulations)
            if errorContents:
                errorFile = csvFile + ".ERROR"
                ff = open(errorFile, 'w')
                ff.write(errorContents)
                ff.close()
                print("Check " + errorFile + " for errors")

    def escape(self, str):
        if re.search(r'[\r\n\",]', str):
            str = '"' + re.sub('"','""',str) + '"'
        return str

    def formatUpdatedSimulationsTable(self, csvFile, simulations):
        # if there is a computed fingerprint, print that instead of existing one
        ff = open(csvFile, 'r')
        lines = ff.readlines()
        ff.close()
        lines.insert(0, '')     # csv line count is 1..n; insert an empty item --> lines[1] is the first line

        containsComputedFingerprint = False
        for simulation in simulations:
            if 'computedFingerprint' in simulation:
                oldFingerprint = simulation['fingerprint']
                newFingerprint = simulation['computedFingerprint']
                oldFpList = oldFingerprint.split(' ')
                if '/' in newFingerprint:
                    # keep old omnetpp4 fp
                    keepFpList = [elem for elem in oldFpList if not '/' in elem]
                    if keepFpList:
                        newFingerprint = ' '.join(keepFpList) + ' ' + newFingerprint
                else:
                    # keep all old omnetpp5 fp
                    keepFpList = [elem for elem in oldFpList if '/' in elem]
                    if keepFpList:
                        newFingerprint = newFingerprint + ' ' + ' '.join(keepFpList)

                if ',' in newFingerprint:
                    newFingerprint = '"' + newFingerprint + '"'
                containsComputedFingerprint = True
                line = simulation['line']
                pattern = "\\b" + oldFingerprint + "\\b"
                (newLine, cnt) = re.subn(pattern, newFingerprint, lines[line])
                if (cnt == 1):
                    lines[line] = newLine
                else:
                    print("ERROR: Cannot replace fingerprint '%s' to '%s' at '%s' line %d:\n     %s" % (oldFingerprint, newFingerprint, csvFile, line, lines[line]))
        return ''.join(lines) if containsComputedFingerprint else None

    def formatFailedSimulationsTable(self, csvFile, simulations):
        ff = open(csvFile, 'r')
        lines = ff.readlines()
        ff.close()
        lines.insert(0, '')     # csv line count is 1..n; insert an empty item --> lines[1] is the first line
        result = []

        containsFailures = False
        for simulation in simulations:
            if 'computedFingerprint' in simulation:
                oldFingerprint = simulation['fingerprint']
                newFingerprint = simulation['computedFingerprint']
                if oldFingerprint != newFingerprint:
                    if not containsFailures:
                        containsFailures = True
                        result.append("# Failures:\n")
                    result.append(lines[simulation['line']])
        return ''.join(result) if containsFailures else None

    def formatErrorSimulationsTable(self, csvFile, simulations):
        ff = open(csvFile, 'r')
        lines = ff.readlines()
        ff.close()
        lines.insert(0, '')     # csv line count is 1..n; insert an empty item --> lines[1] is the first line
        result = []

        containsErrors = False
        for simulation in simulations:
            if 'exitcode' in simulation and simulation['exitcode'] != 0:
                if not containsErrors:
                    containsErrors = True
                    result.append("# Errors:\n")
                result.append(lines[simulation['line']])

        return ''.join(result) if containsErrors else None


def localToRemoteInetPath(localPath):
    return remoteInetRoot + "/" + os.relpath(os.abspath(localPath), localInetRoot)

class RemoteTestSuite(unittest.BaseTestSuite):

    def __init__(self):
        super(RemoteTestSuite,self).__init__()

        # TODO: pass this, and executable as well, if custom
        # testSuite.repeat = args.repeat

        print("connecting to the job queue")
        conn = redis.Redis(host="172.17.0.1")
        self.build_q = rq.Queue("build", connection=conn, default_timeout=30*60)
        self.run_q = rq.Queue("run", connection=conn, default_timeout=30*60)
        

    
    def run(self, result):

        global extraOppRunArgs
        global githash

        result.buffered = True

        print("queueing build job")
        buildJob = self.build_q.enqueue(fingerprints_worker.buildInet, githash)

        print("waiting for build job to end")
        while not (buildJob.is_finished):
            if buildJob.is_failed:
                buildJob.refresh()
                print("ERROR: build job failed " + str(buildJob.exc_info))
                exit(1)
            time.sleep(0.1)
        
        print("build finished: " + str(buildJob.result))

        rng = random.SystemRandom()
        cases = list(self)
        rng.shuffle(cases)

        jobToTest = dict()
        runJobs = []
        for test in cases:
            wd = test.wd

            if not wd.startswith('/'):
                wd = "/" + os.path.relpath(os.path.abspath(test.wd), localInetRoot)


            # run the simulation
            workingdir = _iif(wd.startswith('/'), remoteInetRoot + "/" + wd, wd)

            wdname = '' + wd + ' ' + test.args
            wdname = re.sub('/', '_', wdname)
            wdname = re.sub('[\\W]+', '_', wdname)
            resultdir = workingdir + "/results/" + test.csvFile + "/" + wdname
            
            command = "opp_run_release " + " -n " + nedPath + " -l " + inetLib + " -u Cmdenv " + test.args + \
                _iif(test.simtimelimit != "", " --sim-time-limit=" + test.simtimelimit, "") + \
                " \"--fingerprint=" + test.fingerprint + "\" --cpu-time-limit=" + cpuTimeLimit + \
                " --vector-recording=false --scalar-recording=true" + \
                " --result-dir=" + resultdir + \
                extraOppRunArgs

            runJob = self.run_q.enqueue(fingerprints_worker.runSimulation, githash,
                test.title, command, workingdir, resultdir, depends_on=buildJob)
            
            runJob.meta['title'] = test.title
            runJob.save_meta()

            runJobs.append(runJob)
            jobToTest[runJob] = test


        print("queued " + str(len(runJobs)) + " jobs")

        stop = False
        while runJobs and not stop:
            try:
                for j in runJobs[:]:
                    if j.status is None:
                        print("Test " + j.meta["title"] + " was removed from the queue")
                        runJobs.remove(j)
                    elif j.status == rq.job.JobStatus.FAILED:
                        j.refresh()
                        print("Test " + j.meta["title"] + " failed: " + str(j.exc_info))
                        runJobs.remove(j)
                    else:
                        if j.result is not None:
                            tst = jobToTest[j]
                            result.startTest(tst)
                            
                            try:
                                # process the result
                                # note: fingerprint mismatch is technically NOT an error in 4.2 or before! (exitcode==0)
                                #self.storeExitcodeCallback(result.exitcode)
                                if j.result.exitcode != 0:
                                    raise Exception("runtime error:" + j.result.errormsg)
                                elif j.result.cpuTimeLimitReached:
                                    raise Exception("cpu time limit exceeded")
                                elif j.result.simulatedTime == 0 and self.simtimelimit != '0s':
                                    raise Exception("zero time simulated")
                                elif j.result.isFingerprintOK is None:
                                    raise Exception("other")
                                elif j.result.isFingerprintOK == False:
                                    try:
                                        raise Exception("some fingerprint mismatch; actual  '" + j.result.computedFingerprint + "'")
                                    except:
                                        result.addFailure(tst, sys.exc_info())
                                    anyFingerprintBad = True
                                else:
                                    result.addSuccess(tst)
                                    # fingerprint OK:
                            except:
                                result.addError(tst, sys.exc_info())

                            result.stopTest(jobToTest[j])
                            runJobs.remove(j)

                time.sleep(0.1)
            except KeyboardInterrupt:
                stop = True

        print("remaining: " + str(len(runJobs)) + " jobs")

        return result




class SimulationResult:
    def __init__(self, command, workingdir, exitcode, errorMsg=None, isFingerprintOK=None,
            computedFingerprint=None, simulatedTime=None, numEvents=None, elapsedTime=None, cpuTimeLimitReached=None):
        self.command = command
        self.workingdir = workingdir
        self.exitcode = exitcode
        self.errorMsg = errorMsg
        self.isFingerprintOK = isFingerprintOK
        self.computedFingerprint = computedFingerprint
        self.simulatedTime = simulatedTime
        self.numEvents = numEvents
        self.elapsedTime = elapsedTime
        self.cpuTimeLimitReached = cpuTimeLimitReached


class SimulationTestCase(unittest.TestCase):
    def runSimulation(self, title, command, workingdir, resultdir):
        global logFile
        #os.makedirs(workingdir + "/results", exist_ok=True)


        # run the program and log the output
        t0 = time.time()

        (exitcode, out) = self.runProgram(command, workingdir, resultdir)
        
        elapsedTime = time.time() - t0


        FILE = open(logFile, "a")
        FILE.write("------------------------------------------------------\n"
                 + "Running: " + title + "\n\n"
                 + "$ cd " + workingdir + "\n"
                 + "$ " + command + "\n\n"
                 + out.strip() + "\n\n"
                 + "Exit code: " + str(exitcode) + "\n"
                 + "Elapsed time:  " + str(round(elapsedTime,2)) + "s\n\n")
        FILE.close()

        FILE = open(resultdir + "/test.out", "w")
        FILE.write("------------------------------------------------------\n"
                 + "Running: " + title + "\n\n"
                 + "$ cd " + workingdir + "\n"
                 + "$ " + command + "\n\n"
                 + out.strip() + "\n\n"
                 + "Exit code: " + str(exitcode) + "\n"
                 + "Elapsed time:  " + str(round(elapsedTime,2)) + "s\n\n")
        FILE.close()
        
        result = SimulationResult(command, workingdir, exitcode, elapsedTime=elapsedTime)

        # process error messages
        errorLines = re.findall("<!>.*", out, re.M)
        errorMsg = ""
        for err in errorLines:
            err = err.strip()
            if re.search("Fingerprint", err):
                if re.search("successfully", err):
                    result.isFingerprintOK = True
                else:
                    m = re.search("(computed|calculated): ([-a-zA-Z0-9]+(/[a-z0]+)?)", err)
                    if m:
                        result.isFingerprintOK = False
                        result.computedFingerprint = m.group(2)
                    else:
                        raise Exception("Cannot parse fingerprint-related error message: " + err)
            else:
                errorMsg += "\n" + err
                if re.search("CPU time limit reached", err):
                    result.cpuTimeLimitReached = True
                m = re.search("at event #([0-9]+), t=([0-9]*(\\.[0-9]+)?)", err)
                if m:
                    result.numEvents = int(m.group(1))
                    result.simulatedTime = float(m.group(2))

        result.errormsg = errorMsg.strip()
        return result

    def runProgram(self, command, workingdir, resultdir):
        env = os.environ
#        env['CPUPROFILE'] = resultdir+"/cpuprofile"
#        env['CPUPROFILE_FREQUENCY'] = "1000"


        process = subprocess.Popen(command, shell=True, cwd=workingdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

        out = process.communicate()[0]

        out = re.sub("\r", "", out.decode("utf-8"))


        return (process.returncode, out)

class FingerprintTestCase(SimulationTestCase):
    def __init__(self, title, csvFile, wd, args, simtimelimit, fingerprint, storeFingerprintCallback, storeExitcodeCallback, repeat):
        SimulationTestCase.__init__(self)
        self.title = title
        self.csvFile = csvFile
        self.wd = wd
        self.args = args
        self.simtimelimit = simtimelimit
        self.fingerprint = fingerprint
        self.storeFingerprintCallback = storeFingerprintCallback
        self.storeExitcodeCallback = storeExitcodeCallback
        self.repeat = repeat

    def runTest(self):
        # CPU time limit is a safety guard: fingerprint checks shouldn't take forever
        global localInetRoot, remoteInetRoot, executable, nedPath, cpuTimeLimit, extraOppRunArgs

        # run the simulation
        workingdir = _iif(self.wd.startswith('/'), remoteInetRoot + "/" + self.wd, self.wd)
        wdname = '' + self.wd + ' ' + self.args
        wdname = re.sub('/', '_', wdname)
        wdname = re.sub('[\W]+', '_', wdname)
        resultdir = os.path.abspath(".") + "/results/" + self.csvFile + "/" + wdname
        if not os.path.exists(resultdir):
            try:
                os.makedirs(resultdir)
            except OSError:
                pass
        
        command = executable + " -n " + nedPath + " -l " + inetLib + " -u Cmdenv " + self.args + \
            _iif(self.simtimelimit != "", " --sim-time-limit=" + self.simtimelimit, "") + \
            " \"--fingerprint=" + self.fingerprint + "\" --cpu-time-limit=" + cpuTimeLimit + \
            " --vector-recording=false --scalar-recording=true" + \
            " --result-dir=" + resultdir + \
            extraOppRunArgs
        # print "COMMAND: " + command + '\n'
        anyFingerprintBad = False
        computedFingerprints = set()

        for rep in range(self.repeat):
            result = self.runSimulation(self.title, command, workingdir, resultdir)
            
            # process the result
            # note: fingerprint mismatch is technically NOT an error in 4.2 or before! (exitcode==0)
            self.storeExitcodeCallback(result.exitcode)
            if result.exitcode != 0:
                raise Exception("runtime error:" + result.errormsg)
            elif result.cpuTimeLimitReached:
                raise Exception("cpu time limit exceeded")
            elif result.simulatedTime == 0 and self.simtimelimit != '0s':
                raise Exception("zero time simulated")
            elif result.isFingerprintOK is None:
                raise Exception("other")
            elif result.isFingerprintOK == False:
                computedFingerprints.add(result.computedFingerprint)
                anyFingerprintBad = True
            else:
                # fingerprint OK:
                computedFingerprints.add(self.fingerprint)
#                pass

        if anyFingerprintBad:
            self.storeFingerprintCallback(",".join(computedFingerprints))
            assert False, "some fingerprint mismatch; actual " + " '" + ",".join(computedFingerprints) +"'"

    def __str__(self):
        return self.title


def _iif(cond,t,f):
    return t if cond else f




#
# Copy/paste of TextTestResult, with minor modifications in the output:
# we want to print the error text after ERROR and FAIL, but we don't want
# to print stack traces.
#
class SimulationTextTestResult(unittest.TestResult):
    """A test result class that can print formatted text results to a stream.

    Used by TextTestRunner.
    """
    separator1 = '=' * 70
    separator2 = '-' * 70

    def __init__(self, stream, descriptions, verbosity):
        super(SimulationTextTestResult, self).__init__()
        self.stream = stream
        self.showAll = verbosity > 1
        self.dots = verbosity == 1
        self.descriptions = descriptions

    def getDescription(self, test):
        doc_first_line = test.shortDescription()
        if self.descriptions and doc_first_line:
            return '\n'.join((str(test), doc_first_line))
        else:
            return str(test)

    def startTest(self, test):
        super(SimulationTextTestResult, self).startTest(test)
        if self.showAll:
            self.stream.write(self.getDescription(test))
            self.stream.write(" ... ")
            self.stream.flush()

    def addSuccess(self, test):
        super(SimulationTextTestResult, self).addSuccess(test)
        if self.showAll:
            self.stream.write(": PASS\n")
        elif self.dots:
            self.stream.write('.')
            self.stream.flush()

    def addError(self, test, err):
        # modified
        super(SimulationTextTestResult, self).addError(test, err)
        self.errors[-1] = (test, err[1])  # super class method inserts stack trace; we don't need that, so overwrite it
        if self.showAll:
            (cause, detail) = self._splitMsg(err[1])
            self.stream.write(": ERROR (%s)\n" % cause)
            if detail:
                self.stream.write(detail)
                self.stream.write("\n")
        elif self.dots:
            self.stream.write('E')
            self.stream.flush()
        global exitCode
        exitCode = 1   #"there were errors or failures"

    def addFailure(self, test, err):
        # modified
        super(SimulationTextTestResult, self).addFailure(test, err)
        self.failures[-1] = (test, err[1])  # super class method inserts stack trace; we don't need that, so overwrite it
        if self.showAll:
            (cause, detail) = self._splitMsg(err[1])
            self.stream.write(": FAIL (%s)\n" % cause)
            if detail:
                self.stream.write(detail)
                self.stream.write("\n")
        elif self.dots:
            self.stream.write('F')
            self.stream.flush()
        global exitCode
        exitCode = 1   #"there were errors or failures"

    def addSkip(self, test, reason):
        super(SimulationTextTestResult, self).addSkip(test, reason)
        if self.showAll:
            self.stream.write(": skipped {0!r}".format(reason))
            self.stream.write("\n")
        elif self.dots:
            self.stream.write("s")
            self.stream.flush()

    def addExpectedFailure(self, test, err):
        super(SimulationTextTestResult, self).addExpectedFailure(test, err)
        if self.showAll:
            self.stream.write(": expected failure\n")
        elif self.dots:
            self.stream.write("x")
            self.stream.flush()

    def addUnexpectedSuccess(self, test):
        super(SimulationTextTestResult, self).addUnexpectedSuccess(test)
        if self.showAll:
            self.stream.write(": unexpected success\n")
        elif self.dots:
            self.stream.write("u")
            self.stream.flush()

    def printErrors(self):
        # modified
        if self.dots or self.showAll:
            self.stream.write("\n")
        self.printErrorList('Errors', self.errors)
        self.printErrorList('Failures', self.failures)

    def printErrorList(self, flavour, errors):
        # modified
        if errors:
            self.stream.write("%s:\n" % flavour)
        for test, err in errors:
            self.stream.write("  %s (%s)\n" % (self.getDescription(test), self._splitMsg(err)[0]))

    def _splitMsg(self, msg):
        cause = str(msg)
        detail = None
        if cause.count(': '):
            (cause, detail) = cause.split(': ',1)
        return (cause, detail)








global githash
if __name__ == "__main__":
    global githash
    parser = argparse.ArgumentParser(description='Run the fingerprint tests specified in the input files.')
    parser.add_argument('gitref', metavar='gitref')
    parser.add_argument('testspecfiles', nargs='*', metavar='testspecfile', help='CSV files that contain the tests to run (default: *.csv). Expected CSV file columns: workingdir, args, simtimelimit, fingerprint')
    parser.add_argument('-m', '--match', action='append', metavar='regex', help='Line filter: a line (more precisely, workingdir+SPACE+args) must match any of the regular expressions in order for that test case to be run')
    parser.add_argument('-x', '--exclude', action='append', metavar='regex', help='Negative line filter: a line (more precisely, workingdir+SPACE+args) must NOT match any of the regular expressions in order for that test case to be run')
    parser.add_argument('-r', '--repeat', type=int, default=1, help='number of repeating each test (default: 1)')
    parser.add_argument('-a', '--oppargs', action='append', metavar='oppargs', nargs='*', help='extra opp_run arguments without leading "--", you can terminate list with " -- " ')
    parser.add_argument('-e', '--executable', help='Determines which binary to execute (e.g. opp_run_dbg, opp_run_release)')
    args = parser.parse_args()

    if os.path.isfile(logFile):
        FILE = open(logFile, "w")
        FILE.close()

    githash = args.gitref

    if not args.testspecfiles:
        args.testspecfiles = glob.glob('*.csv')

    if args.oppargs:
        for oppArgList in args.oppargs:
            for oppArg in oppArgList:
                extraOppRunArgs += " --" + oppArg

    if args.executable:
        executable = args.executable

    generator = FingerprintTestCaseGenerator()
    testcases = generator.generateFromCSV(args.testspecfiles, args.match, args.exclude, args.repeat)


    testSuite = RemoteTestSuite()
    testSuite.addTests(testcases)
    testSuite.repeat = args.repeat

    testRunner = unittest.TextTestRunner(stream=sys.stdout, verbosity=9, resultclass=SimulationTextTestResult)

    testRunner.run(testSuite)


    generator.writeUpdatedCSVFiles()
    generator.writeErrorCSVFiles()
    generator.writeFailedCSVFiles()

    print("Log has been saved to %s" % logFile)

    exit(exitCode)
