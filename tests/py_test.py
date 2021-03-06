from scannerpy import (
    Database, Config, DeviceType, ColumnType, BulkJob, Job, ProtobufGenerator)
from scannerpy.stdlib import parsers
import tempfile
import toml
import pytest
from subprocess import check_call as run
from multiprocessing import Process, Queue
import requests
import imp
import os.path
import socket
import numpy as np
import sys
import grpc
import struct

try:
    run(['nvidia-smi'])
    has_gpu = True
except OSError:
    has_gpu = False

gpu = pytest.mark.skipif(
    not has_gpu,
    reason='need GPU to run')
slow = pytest.mark.skipif(
    not pytest.config.getoption('--runslow'),
    reason='need --runslow option to run')

cwd = os.path.dirname(os.path.abspath(__file__))

@slow
def test_tutorial():
    def run_py(path):
        print(path)
        run(
            'cd {}/../examples/tutorial && python {}.py'.format(cwd, path),
            shell=True)

    run(
        'cd {}/../examples/tutorial/resize_op && '
        'mkdir -p build && cd build && cmake -D SCANNER_PATH={} .. && '
        'make'.format(cwd, cwd + '/..'),
        shell=True)

    tutorials = [
        '00_basic',
        '01_sampling',
        '02_collections',
        '03_ops',
        '04_compression',
        '05_custom_op']

    for t in tutorials:
        run_py(t)

@slow
def test_examples():
    def run_py((d, f)):
        print(f)
        run('cd {}/../examples/{} && python {}.py'.format(cwd, d, f),
            shell=True)

    examples = [
        ('face_detection', 'face_detect'),
        ('shot_detection', 'shot_detect')]

    for e in examples:
        run_py(e)

@pytest.fixture(scope="module")
def db():
    # Create new config
    with tempfile.NamedTemporaryFile(delete=False) as f:
        cfg = Config.default_config()
        cfg['storage']['db_path'] = tempfile.mkdtemp()
        f.write(toml.dumps(cfg))
        cfg_path = f.name

    # Setup and ingest video
    with Database(config_path=cfg_path, debug=True) as db:
        # Download video from GCS
        url = "https://storage.googleapis.com/scanner-data/test/short_video.mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as f:
            host = socket.gethostname()
            # HACK: special proxy case for Ocean cluster
            if host in ['ocean', 'crissy', 'pismo', 'stinson']:
                resp = requests.get(url, stream=True, proxies={
                    'https': 'http://proxy.pdl.cmu.edu:3128/'
                })
            else:
                resp = requests.get(url, stream=True)
            assert resp.ok
            for block in resp.iter_content(1024):
                f.write(block)
            vid1_path = f.name

        # Make a second one shorter than the first
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as f:
            vid2_path = f.name
        run(['ffmpeg', '-y', '-i', vid1_path, '-ss', '00:00:00', '-t',
             '00:00:10', '-c:v', 'libx264', '-strict', '-2', vid2_path])

        db.ingest_videos([('test1', vid1_path), ('test2', vid2_path)])

        yield db

        # Tear down
        run(['rm', '-rf',
            cfg['storage']['db_path'],
            cfg_path,
            vid1_path,
            vid2_path])

def test_new_database(db): pass

def test_table_properties(db):
    table = db.table('test1')
    assert table.id() == 0
    assert table.name() == 'test1'
    assert table.num_rows() == 720
    assert [c for c in table.column_names()] == ['index', 'frame']

def test_collection(db):
    c = db.new_collection('test', [db.table('test1'), db.table('test2')])

    frame = db.ops.FrameInput()
    hist = db.ops.Histogram(frame = frame)
    output = db.ops.Output(columns=[hist])

    jobs = []
    for table in c.tables():
        job = Job(op_args={
            frame: table.column('frame'),
            output: table.name() + '_ignore',
        })
        jobs.append(job)

    bulk_job = BulkJob(output=output, jobs=jobs)
    db.run(bulk_job, show_progress=False, force=True)
    db.delete_collection('test')

def test_summarize(db):
    db.summarize()

def test_load_video_column(db):
    next(db.table('test1').load(['frame']))
    # Gather rows
    rows = [0, 10, 100, 200]
    frames = [_ for _ in db.table('test1').load(['frame'], rows=rows)]
    assert len(frames) == len(rows)

def test_load_video_column(db):
    next(db.table('test1').load(['frame']))

def test_profiler(db):
    frame = db.ops.FrameInput()
    hist = db.ops.Histogram(frame=frame)
    output_op = db.ops.Output(columns=[hist])

    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            output_op: '_ignore'
        }
    )
    bulk_job = BulkJob(output=output_op, jobs=[job])

    output = db.run(bulk_job, show_progress=False, force=True)
    profiler = output[0].profiler()
    f = tempfile.NamedTemporaryFile(delete=False)
    f.close()
    profiler.write_trace(f.name)
    profiler.statistics()
    run(['rm', '-f', f.name])


def test_sample(db):
    def run_sampler_job(sampler_args, expected_rows):
        frame = db.ops.FrameInput()
        sample_frame = frame.sample()
        output_op = db.ops.Output(columns=[sample_frame])

        job = Job(
            op_args={
                frame: db.table('test1').column('frame'),
                sample_frame: sampler_args,
                output_op: 'test_sample',
            }
        )
        bulk_job = BulkJob(output=output_op, jobs=[job])
        tables = db.run(bulk_job, force=True, show_progress=False)
        num_rows = 0
        for (frame_index, _) in tables[0].column('frame').load():
            num_rows += 1
        assert num_rows == expected_rows

    # Stride
    expected = (db.table('test1').num_rows() + 8 - 1) / 8
    run_sampler_job(db.sampler.strided(8), expected)
    # Range
    run_sampler_job(db.sampler.range(0, 30), 30)
    # Strided Range
    run_sampler_job(db.sampler.strided_range(0, 300, 10), 30)
    # Gather
    run_sampler_job(db.sampler.gather([0, 150, 377, 500]), 4)

def test_space(db):
    def run_spacer_job(spacing_args):
        frame = db.ops.FrameInput()
        hist = db.ops.Histogram(frame=frame)
        space_hist = hist.space()
        output_op = db.ops.Output(columns=[space_hist])

        job = Job(
            op_args={
                frame: db.table('test1').column('frame'),
                space_hist: spacing_args,
                output_op: 'test_space',
            }
        )
        bulk_job = BulkJob(output=output_op, jobs=[job])
        tables = db.run(bulk_job, force=True, show_progress=False)
        return tables[0]

    # Repeat
    spacing_distance = 8
    table = run_spacer_job(db.sampler.space_repeat(spacing_distance))
    num_rows = 0
    for (frame_index, hist) in table.load(['histogram'], parsers.histograms):
        # Verify outputs are repeated correctly
        if num_rows % spacing_distance == 0:
            ref_hist = hist
        assert len(hist) == 3
        for c in range(len(hist)):
            assert (ref_hist[c] == hist[c]).all()
        num_rows += 1
    assert num_rows == db.table('test1').num_rows() * spacing_distance

    # Null
    table = run_spacer_job(db.sampler.space_null(spacing_distance))
    num_rows = 0
    for (frame_index, hist) in table.load(['histogram'], parsers.histograms):
        # Verify outputs are None for null rows
        if num_rows % spacing_distance == 0:
            assert hist is not None
            assert len(hist) == 3
            assert hist[0].shape[0] == 16
        else:
            assert hist is None
        num_rows += 1
    assert num_rows == db.table('test1').num_rows() * spacing_distance

def test_slicing(db):
    frame = db.ops.FrameInput()
    slice_frame = frame.slice()
    unsliced_frame = slice_frame.unslice()
    output_op = db.ops.Output(columns=[unsliced_frame])
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            slice_frame: db.partitioner.all(50),
            output_op: 'test_slicing',
        }
    )
    bulk_job = BulkJob(output=output_op, jobs=[job])
    tables = db.run(bulk_job, force=True, show_progress=False)

    num_rows = 0
    for (frame_index, _) in tables[0].column('frame').load():
        num_rows += 1
    assert num_rows == db.table('test1').num_rows()

def test_bounded_state(db):
    warmup = 3

    frame = db.ops.FrameInput()
    increment = db.ops.TestIncrementBounded(ignore=frame, warmup=warmup)
    sampled_increment = increment.sample()
    output_op = db.ops.Output(columns=[sampled_increment])
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            sampled_increment: db.sampler.gather([0, 10, 25, 26, 27]),
            output_op: 'test_slicing',
        }
    )
    bulk_job = BulkJob(output=output_op, jobs=[job])
    tables = db.run(bulk_job, force=True, show_progress=False)

    num_rows = 0
    expected_output = [0, warmup, warmup, warmup + 1, warmup + 2]
    for (frame_index, buf) in tables[0].column('integer').load():
        (val,) = struct.unpack('=q', buf)
        assert val == expected_output[num_rows]
        print(num_rows)
        num_rows += 1
    assert num_rows == 5

def test_unbounded_state(db):
    frame = db.ops.FrameInput()
    slice_frame = frame.slice()
    increment = db.ops.TestIncrementUnbounded(ignore=slice_frame)
    unsliced_increment = increment.unslice()
    output_op = db.ops.Output(columns=[unsliced_increment])
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            slice_frame: db.partitioner.all(50),
            output_op: 'test_slicing',
        }
    )
    bulk_job = BulkJob(output=output_op, jobs=[job])
    tables = db.run(bulk_job, force=True, show_progress=False)

    num_rows = 0
    for (frame_index, buf) in tables[0].column('integer').load():
        (val,) = struct.unpack('=q', buf)
        assert val == frame_index % 50
        num_rows += 1
    assert num_rows == db.table('test1').num_rows()


def builder(cls):
    inst = cls()

    class Generated:
        def test_cpu(self, db):
            inst.run(db, inst.job(db, DeviceType.CPU))

        @gpu
        def test_gpu(self, db):
            inst.run(db, inst.job(db, DeviceType.GPU))

    return Generated

@builder
class TestHistogram:
    def job(self, db, ty):
        frame = db.ops.FrameInput()
        hist = db.ops.Histogram(frame=frame, device=ty)
        output_op = db.ops.Output(columns=[hist])

        job = Job(
            op_args={
                frame: db.table('test1').column('frame'),
                output_op: 'test_hist'
            }
        )
        bulk_job = BulkJob(output=output_op, jobs=[job])
        return bulk_job

    def run(self, db, job):
        tables = db.run(job, force=True, show_progress=False)
        next(tables[0].load(['histogram'], parsers.histograms))

@builder
class TestOpticalFlow:
    def job(self, db, ty):
        frame = db.ops.FrameInput()
        flow = db.ops.OpticalFlow(
            frame = frame,
            stencil = [-1, 0],
            device = ty)
        flow_range = flow.sample()
        out = db.ops.Output(columns=[flow_range])
        job = Job(op_args={
            frame: db.table('test1').column('frame'),
            flow_range: db.sampler.range(0, 50),
            out: 'test_flow',
        })
        return BulkJob(output=out, jobs=[job])

    def run(self, db, job):
        [table] = db.run(job, force=True, show_progress=False)
        num_rows = 0
        for (frame_index, _) in table.column('flow').load():
            num_rows += 1
        assert num_rows == 50

        fid, flows = next(table.load(['flow']))
        flow_array = flows[0]
        assert fid == 0
        assert flow_array.dtype == np.float32
        assert flow_array.shape[0] == 480
        assert flow_array.shape[1] == 640
        assert flow_array.shape[2] == 2

def test_python_kernel(db):
    db.register_op('TestPy',
                   [('frame', ColumnType.Video)],
                   ['dummy'])
    db.register_python_kernel('TestPy', DeviceType.CPU,
                              cwd + '/test_py_kernel.py')

    frame = db.ops.FrameInput()
    range_frame = frame.sample()
    test_out = db.ops.TestPy(frame=range_frame)
    output_op = db.ops.Output(columns=[test_out])
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            range_frame: db.sampler.range(0, 30),
            output_op: 'test_hist'
        }
    )
    bulk_job = BulkJob(output=output_op, jobs=[job])

    tables = db.run(bulk_job, force=True, show_progress=False)
    next(tables[0].load(['dummy']))

def test_python_batch_kernel(db):
    db.register_op('TestPyBatch',
                   [('frame', ColumnType.Video)],
                   ['dummy'])
    db.register_python_kernel('TestPyBatch', DeviceType.CPU,
                              cwd + '/test_py_batch_kernel.py', batch=10)

    frame = db.ops.FrameInput()
    range_frame = frame.sample()
    test_out = db.ops.TestPyBatch(frame=range_frame, batch=50)
    output_op = db.ops.Output(columns=[test_out])
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            range_frame: db.sampler.range(0, 30),
            output_op: 'test_hist'
        }
    )
    bulk_job = BulkJob(output=output_op, jobs=[job])

    tables = db.run(bulk_job, force=True, show_progress=False)
    next(tables[0].load(['dummy']))

def test_blur(db):
    frame = db.ops.FrameInput()
    range_frame = frame.sample()
    blurred_frame = db.ops.Blur(frame=range_frame, kernel_size=3, sigma=0.1)
    output_op = db.ops.Output(columns=[blurred_frame])
    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            range_frame: db.sampler.range(0, 30),
            output_op: 'test_blur',
        }
    )
    bulk_job = BulkJob(output=output_op, jobs=[job])
    tables = db.run(bulk_job, force=True, show_progress=False)
    table = tables[0]

    fid, frames = next(table.load(['frame']))
    frame_array = frames[0]
    assert fid == 0
    assert frame_array.dtype == np.uint8
    assert frame_array.shape[0] == 480
    assert frame_array.shape[1] == 640
    assert frame_array.shape[2] == 3

def test_lossless(db):
    frame = db.ops.FrameInput()
    range_frame = frame.sample()
    blurred_frame = db.ops.Blur(frame=range_frame, kernel_size=3, sigma=0.1)
    output_op = db.ops.Output(columns=[blurred_frame.lossless()])

    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            range_frame: db.sampler.range(0, 30),
            output_op: 'test_blur_lossless'
        }
    )
    bulk_job = BulkJob(output=output_op, jobs=[job])
    tables = db.run(bulk_job, force=True, show_progress=False)
    table = tables[0]
    next(table.load(['frame']))

def test_compress(db):
    frame = db.ops.FrameInput()
    range_frame = frame.sample()
    blurred_frame = db.ops.Blur(frame=range_frame, kernel_size=3, sigma=0.1)
    compressed_frame = blurred_frame.compress(
        'video', bitrate = 1 * 1024 * 1024)
    output_op = db.ops.Output(columns=[compressed_frame])

    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            range_frame: db.sampler.range(0, 30),
            output_op: 'test_blur_compressed'
        }
    )
    bulk_job = BulkJob(output=output_op, jobs=[job])
    tables = db.run(bulk_job, force=True, show_progress=False)
    table = tables[0]
    next(table.load(['frame']))

def test_save_mp4(db):
    frame = db.ops.FrameInput()
    range_frame = frame.sample()
    blurred_frame = db.ops.Blur(frame=range_frame, kernel_size=3, sigma=0.1)
    output_op = db.ops.Output(columns=[blurred_frame])

    job = Job(
        op_args={
            frame: db.table('test1').column('frame'),
            range_frame: db.sampler.range(0, 30),
            output_op: 'test_save_mp4'
        }
    )
    bulk_job = BulkJob(output=output_op, jobs=[job])
    tables = db.run(bulk_job, force=True, show_progress=False)
    table = tables[0]
    f = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
    f.close()
    table.column('frame').save_mp4(f.name)
    run(['rm', '-rf', f.name])

@pytest.fixture()
def fault_db():
    # Create new config
    #with tempfile.NamedTemporaryFile(delete=False) as f:
    with open('/tmp/config_test', 'w') as f:
        cfg = Config.default_config()
        cfg['storage']['db_path'] = tempfile.mkdtemp()
        cfg['network']['master_port'] = '5005'
        cfg['network']['worker_port'] = '5006'
        f.write(toml.dumps(cfg))
        cfg_path = f.name

    # Setup and ingest video
    with Database(config_path=cfg_path) as db:
        # Download video from GCS
        url = "https://storage.googleapis.com/scanner-data/test/short_video.mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as f:
            host = socket.gethostname()
            # HACK: special proxy case for Ocean cluster
            if host in ['ocean', 'crissy', 'pismo', 'stinson']:
                resp = requests.get(url, stream=True, proxies={
                    'https': 'http://proxy.pdl.cmu.edu:3128/'
                })
            else:
                resp = requests.get(url, stream=True)
            assert resp.ok
            for block in resp.iter_content(1024):
                f.write(block)
            vid1_path = f.name

        # Make a second one shorter than the first
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as f:
            vid2_path = f.name
        run(['ffmpeg', '-y', '-i', vid1_path, '-ss', '00:00:00', '-t',
             '00:00:10', '-c:v', 'libx264', '-strict', '-2', vid2_path])

        db.ingest_videos([('test1', vid1_path), ('test2', vid2_path)])

        yield db

        # Tear down
        run(['rm', '-rf',
            cfg['storage']['db_path'],
            cfg_path,
            vid1_path,
            vid2_path])


# def test_clean_worker_shutdown(fault_db):
#     spawn_port = 5010
#     def worker_shutdown_task(config, master_address, worker_address):
#         from scannerpy import ProtobufGenerator, Config, start_worker
#         import time
#         import grpc
#         import subprocess

#         c = Config(None)

#         import scanner.metadata_pb2 as metadata_types
#         import scanner.engine.rpc_pb2 as rpc_types
#         import scanner.types_pb2 as misc_types
#         import libscanner as bindings

#         protobufs = ProtobufGenerator(config)

#         # Wait to kill worker
#         time.sleep(8)
#         # Kill worker
#         channel = grpc.insecure_channel(
#             worker_address,
#             options=[('grpc.max_message_length', 24499183 * 2)])
#         worker = protobufs.WorkerStub(channel)

#         try:
#             worker.Shutdown(protobufs.Empty())
#         except grpc.RpcError as e:
#             status = e.code()
#             if status == grpc.StatusCode.UNAVAILABLE:
#                 print('could not shutdown worker!')
#                 exit(1)
#             else:
#                 raise ScannerException('Worker errored with status: {}'
#                                        .format(status))

#         # Wait a bit
#         time.sleep(15)
#         script_dir = os.path.dirname(os.path.realpath(__file__))
#         subprocess.call(['python ' +  script_dir + '/spawn_worker.py'],
#                         shell=True)

#     master_addr = fault_db._master_address
#     worker_addr = fault_db._worker_addresses[0]
#     shutdown_process = Process(target=worker_shutdown_task,
#                              args=(fault_db.config, master_addr, worker_addr))
#     shutdown_process.daemon = True
#     shutdown_process.start()

#     frame = fault_db.ops.FrameInput()
#     range_frame = frame.sample()
#     sleep_frame = fault_db.ops.SleepFrame(ignore = range_frame)
#     output_op = fault_db.ops.Output(columns=[sleep_frame])

#     job = Job(
#         op_args={
#             frame: fault_db.table('test1').column('frame'),
#             range_frame: fault_db.sampler.range(0, 15),
#             output_op: 'test_shutdown',
#         }
#     )
#     bulk_job = BulkJob(output=output_op, jobs=[job])
#     table = fault_db.run(bulk_job, pipeline_instances_per_node=1, force=True,
#                          show_progress=False)
#     table = table[0]
#     assert len([_ for _, _ in table.column('dummy').load()]) == 15

#     # Shutdown the spawned worker
#     channel = grpc.insecure_channel(
#         'localhost:' + str(spawn_port),
#         options=[('grpc.max_message_length', 24499183 * 2)])
#     worker = fault_db.protobufs.WorkerStub(channel)

#     try:
#         worker.Shutdown(fault_db.protobufs.Empty())
#     except grpc.RpcError as e:
#         status = e.code()
#         if status == grpc.StatusCode.UNAVAILABLE:
#             print('could not shutdown worker!')
#             exit(1)
#         else:
#             raise ScannerException('Worker errored with status: {}'
#                                    .format(status))
#     shutdown_process.join()


# def test_fault_tolerance(fault_db):
#     spawn_port = 5010
#     def worker_killer_task(config, master_address, worker_address):
#         from scannerpy import ProtobufGenerator, Config, start_worker
#         import time
#         import grpc
#         import subprocess
#         import signal
#         import os

#         c = Config(None)

#         import scanner.metadata_pb2 as metadata_types
#         import scanner.engine.rpc_pb2 as rpc_types
#         import scanner.types_pb2 as misc_types
#         import libscanner as bindings

#         protobufs = ProtobufGenerator(config)

#         # Kill worker
#         channel = grpc.insecure_channel(
#             worker_address,
#             options=[('grpc.max_message_length', 24499183 * 2)])
#         worker = protobufs.WorkerStub(channel)

#         try:
#             worker.Shutdown(protobufs.Empty())
#         except grpc.RpcError as e:
#             status = e.code()
#             if status == grpc.StatusCode.UNAVAILABLE:
#                 print('could not shutdown worker!')
#                 exit(1)
#             else:
#                 raise ScannerException('Worker errored with status: {}'
#                                        .format(status))

#         # Spawn a worker that we will force kill
#         script_dir = os.path.dirname(os.path.realpath(__file__))
#         with open(os.devnull, 'w') as fp:
#             p = subprocess.Popen(
#                 ['python ' +  script_dir + '/spawn_worker.py'],
#                 shell=True,
#                 stdout=fp, stderr=fp,
#                 preexec_fn=os.setsid)

#             # Wait a bit for the worker to do its thing
#             time.sleep(10)

#             # Force kill worker process to trigger fault tolerance
#             os.killpg(os.getpgid(p.pid), signal.SIGTERM) 
#             p.communicate()

#             # Wait for fault tolerance to kick in
#             time.sleep(25)

#             # Spawn the worker again
#             subprocess.call(['python ' +  script_dir + '/spawn_worker.py'],
#                             shell=True)

#     master_addr = fault_db._master_address
#     worker_addr = fault_db._worker_addresses[0]
#     killer_process = Process(target=worker_killer_task,
#                              args=(fault_db.config, master_addr, worker_addr))
#     killer_process.daemon = True
#     killer_process.start()

#     frame = fault_db.ops.FrameInput()
#     range_frame = frame.sample()
#     sleep_frame = fault_db.ops.SleepFrame(ignore = range_frame)
#     output_op = fault_db.ops.Output(columns=[sleep_frame])

#     job = Job(
#         op_args={
#             frame: fault_db.table('test1').column('frame'),
#             range_frame: fault_db.sampler.range(0, 20),
#             output_op: 'test_fault',
#         }
#     )
#     bulk_job = BulkJob(output=output_op, jobs=[job])
#     table = fault_db.run(bulk_job, pipeline_instances_per_node=1, force=True,
#                          show_progress=False)
#     table = table[0]

#     assert len([_ for _, _ in table.column('dummy').load()]) == 20

#     # Shutdown the spawned worker
#     channel = grpc.insecure_channel(
#         'localhost:' + str(spawn_port),
#         options=[('grpc.max_message_length', 24499183 * 2)])
#     worker = fault_db.protobufs.WorkerStub(channel)

#     try:
#         worker.Shutdown(fault_db.protobufs.Empty())
#     except grpc.RpcError as e:
#         status = e.code()
#         if status == grpc.StatusCode.UNAVAILABLE:
#             print('could not shutdown worker!')
#             exit(1)
#         else:
#             raise ScannerException('Worker errored with status: {}'
#                                    .format(status))
#     killer_process.join()
