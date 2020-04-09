from __future__ import print_function

from multiprocessing.pool import ThreadPool
import queue
import subprocess
import sys
import time
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import utils

THROUGHPUT_ESTIMATION_INTERVAL = 10

class Dispatcher:
    def __init__(self, round_duration, gpu_ids, worker_rpc_client,
                 run_dir, checkpoint_dir, write_queue):
        self._thread_pool = ThreadPool()
        self._round_duration = round_duration
        self._worker_rpc_client = worker_rpc_client
        self._run_dir = run_dir
        self._checkpoint_dir = checkpoint_dir
        self._gpu_ids = gpu_ids
        self._gpu_queue = queue.Queue(len(self._gpu_ids))
        self._write_queue = write_queue
        for gpu_id in self._gpu_ids:
            self._gpu_queue.put(gpu_id)

    def _construct_command(self, job, gpu_id):
        checkpoint_dir = os.path.join(self._checkpoint_dir,
                                      'job_id=%d' % (job.job_id))
        if not os.path.isdir(checkpoint_dir):
            os.mkdir(checkpoint_dir)

        if job.needs_data_dir:
            command = job.command % (self._run_dir, self._run_dir)
        else:
            command = job.command % (self._run_dir)

        command = '%s --local_rank %d' % (command, gpu_id)
        command = '%s --throughput_estimation_interval %d' % (
                    command, THROUGHPUT_ESTIMATION_INTERVAL)

        command = ('%s %s %d --checkpoint_dir %s '
                   '--max_duration %d') % (command,
                                           job.num_steps_arg,
                                           job.total_steps,
                                           checkpoint_dir,
                                           self._round_duration)
        return command

    def launch_job(self, job, command, gpu_id):
        start_time = time.time()
        output = ''
        try:
            proc = subprocess.run(command,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT,
                                  shell=True)
            execution_time = time.time() - start_time
            output = proc.stdout.decode('utf-8').strip()
            self._write_queue.put('Job ID: %s, '
                                  'Num_steps: %d, '
                                  'Execution time: %.3f seconds, '
                                  'Output:\n%s' % (str(job.job_id),
                                                   job.total_steps,
                                                   execution_time,
                                                   output))

        except subprocess.CalledProcessError as e:
            error_message =\
                'Job %s failed with error code %s' % (str(job.job_id),
                                                      str(e.returncode))
            if e.args is not None:
                error_message += '\nArgs: %s' % (str(e.args))
            if e.stdout is not None:
                error_message += 'Stdout: %s' % (e.stdout)
            if e.stderr is not None:
                error_message += 'Stderr: %s' % (e.stderr)
            self._write_queue.put(error_message)
            execution_time = -1
            output = ''
        except Exception as e:
            self._write_queue.put('Dispatcher failed: %s' % (e))
            execution_time = -1
            output = ''

        return [job.job_id, execution_time, output]

    def _dispatch_jobs_helper(self, jobs, worker_id):
        gpu_id = self._gpu_queue.get()
        commands = [self._construct_command(job, gpu_id) for job in jobs]
        results = []
        for job, command in zip(jobs, commands):
            self._write_queue.put('Running job %d on GPU %d, '
                                  'command: "%s"' % (job.job_id, gpu_id,
                                                     command))
            results.append(self._thread_pool.apply_async(self.launch_job,
                                                         (job, command,
                                                          gpu_id,)))
        job_descriptions = [result.get() for result in results]
        self._gpu_queue.put(gpu_id)
        outputs = [x[-1] for x in job_descriptions]
        all_num_steps = utils.get_steps_from_job_output(outputs)
        for i in range(len(all_num_steps)):
            job_descriptions[i][-1] = all_num_steps[i]
        if len(jobs) == 1:
            self._write_queue.put('Job %d has completed, '
                                  'notifying scheduler...' % (jobs[0].job_id))
        else:
            job_ids = [job.job_id for job in jobs]
            self._write_queue.put('Jobs %s have completed, '
                                  'notifying scheduler...' % (str(job_ids)))
        self._worker_rpc_client.notify_scheduler(worker_id,
                                                 job_descriptions)

    def dispatch_jobs(self, jobs, worker_id):
        self._thread_pool.apply_async(self._dispatch_jobs_helper,
                                      (jobs, worker_id,))

    def shutdown(self):
        self._thread_pool.terminate()
