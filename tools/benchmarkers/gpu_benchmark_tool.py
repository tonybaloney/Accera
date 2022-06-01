#!/usr/bin/env python3
####################################################################################################
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See LICENSE in the project root for license information.
####################################################################################################

import csv
from io import StringIO
from itertools import islice
import os
from typing import List
import argparse
import sys
import subprocess
import accera_gemm
import cosmosdb
import re
import gemm_opts
import git
import shutil
from datetime import datetime

def get_current_commit_id():
    repo = git.Repo(search_parent_directories=True)
    return repo.head.object.hexsha

def get_current_commit_datetime():
    repo = git.Repo(search_parent_directories=True)
    return repo.head.object.committed_datetime

def get_current_branch():
    repo = git.Repo(search_parent_directories=True)
    return repo.active_branch.name

def exec_ext_benchmarker(gpu_id: int, gemm: gemm_opts.GemmOpts, benchmark_tool):
    proc = subprocess.run([benchmark_tool] + list(
        map(str, [gemm.type, gemm.m, gemm.n, gemm.k, int(gemm.transA), int(gemm.transB), gemm.alpha, gemm.beta, gemm.lda, gemm.ldb, gemm.ldc, gpu_id])),
        capture_output=True,
        text=True)
    proc.check_returncode()
    return proc.stdout

def benchmark_gemm_shapes(data: List[gemm_opts.GemmOpts], git_branch: str, target_name: str, output_prefix: str, rocblas: str, composable_kernel: str, cublas: str, cutlass: str, available_gpus, containerName, verboseLogs, checkResult, compilerVersion, deviceProperties):
    result_dir = os.path.split(output_prefix)[0] or '.'
    if not os.path.isdir(result_dir):
        os.makedirs(result_dir)

    commit_id = get_current_commit_id()
    commit_datetime = get_current_commit_datetime()
    commit_branch = git_branch.replace("refs/heads/", "") if git_branch else get_current_branch()

    if rocblas or cublas:
        benchmark_tool_name = 'rocblas' if rocblas else 'cublas'
        benchmark_tool = rocblas if rocblas else cublas
        print(f'Running {benchmark_tool_name} baseline benchmarks')

        resultRows = []
        for gemm in data:
            best_time = 0.0
            best_throughput = 0.0
            gpu = 0
            prog_out = ''
            for gpu_id in range(len(available_gpus)):
                if available_gpus[gpu_id]:
                    print(f"Processing input: {gemm} on GPU {gpu_id}")
                    output = exec_ext_benchmarker(gpu_id, gemm, benchmark_tool)
                    print(output)
                    tokens = output.split(",")
                    if float(tokens[len(tokens) - 1].rstrip()) > best_throughput:
                        best_throughput = float(tokens[len(tokens) - 1].rstrip())
                        best_time = tokens[len(tokens) - 2]
                        gpu = gpu_id
                        prog_out = output
            else:
                benchmarkResult = accera_gemm.BenchmarkResult(opts=gemm, gpu_id=gpu, commit_id=commit_id, commit_datetime=commit_datetime, commit_branch=commit_branch, target_name=target_name, deviceProperties=deviceProperties[gpu])
                benchmarkResult.compiler_version = compilerVersion
                benchmarkResult.target_rt = 'ROCM' if rocblas else 'CUDA'
                benchmarkResult.compilable = True
                benchmarkResult.executable = True
                benchmarkResult.time_ms = best_time
                benchmarkResult.TFlops = str(best_throughput)
                benchmarkResult.prog_out = prog_out
                resultRows.append(benchmarkResult.getResultRow())
        else:
            cosmosdb.upsert_benchmark_results(resultRows, benchmark_tool_name, verboseLogs)
            cosmosdb.show_benchmark_summary(benchmark_tool_name)
    elif composable_kernel:
        print('Running composable_kernel baseline benchmarks')
        resultRows = []
        for gemm in data:
            print(f"Processing input: {gemm} on GPU 0")
            datatype = '0' if gemm.type == 's' else '1'
            if gemm.transA and gemm.transB:
                layout = '3'
            elif gemm.transA and not gemm.transB:
                layout = '2'
            elif not gemm.transA and gemm.transB:
                layout = '1'
            else:
                layout = '0'
            lda = gemm.m if not gemm.transA else gemm.k
            ldb = gemm.k if not gemm.transB else gemm.n
            ldc = gemm.m
            benchmarkResult = accera_gemm.BenchmarkResult(opts=gemm, gpu_id=0, commit_id=commit_id, commit_datetime=commit_datetime, commit_branch=commit_branch, target_name=target_name, deviceProperties=deviceProperties[0])
            proc = subprocess.run([composable_kernel, 'gemm', datatype, layout, '0', '0', '0', '2', str(gemm.m), str(gemm.n), str(gemm.k), str(lda), str(ldb), str(ldc)], capture_output=True, text=True)
            benchmarkResult.compiler_version = compilerVersion
            benchmarkResult.target_rt = 'ROCM'
            benchmarkResult.compilable = True
            benchmarkResult.executable = True
            benchmarkResult.prog_out = proc.stdout
            matches = re.search('Best Perf: (.+) ms, (.+) TFlops', proc.stdout)
            if matches:
                benchmarkResult.time_ms = matches.group(1)
                benchmarkResult.TFlops = matches.group(2)
                print(matches.group(0))
                resultRows.append(benchmarkResult.getResultRow())
        else:
            cosmosdb.upsert_benchmark_results(resultRows, "composable_kernel", verboseLogs)
            cosmosdb.show_benchmark_summary("composable_kernel")
    elif cutlass:
        print('Running CUTLASS baseline benchmarks')
        resultRows = []
        for gemm in data:
            print(f"Processing input: {gemm} on GPU 0")
            datatype = 'f32' if gemm.type == 's' else 'f16'
            layoutA = 't' if gemm.transA else 'n'
            layoutB = 't' if gemm.transB else 'n'
            result_filename = f'{output_prefix}_cutlass'
            benchmarkResult = accera_gemm.BenchmarkResult(opts=gemm, gpu_id=0, commit_id=commit_id, commit_datetime=commit_datetime, commit_branch=commit_branch, target_name=target_name, deviceProperties=deviceProperties[0])
            proc = subprocess.run([cutlass, '--operation=Gemm', f'--A={datatype}:{layoutA}', f'--B={datatype}:{layoutB}', f'--C={datatype}:*', f'--m={gemm.m}', f'--n={gemm.n}', f'--k={gemm.k}',
                                   f'--alpha={gemm.alpha}', f'--beta={gemm.beta}', '--op_class=tensorop', f'--output={result_filename}'], capture_output=True, text=True)
            print(proc.stdout)
            benchmarkResult.compiler_version = compilerVersion
            benchmarkResult.target_rt = 'CUDA'
            benchmarkResult.compilable = True
            benchmarkResult.executable = True
            benchmarkResult.prog_out = proc.stdout

            # Read the cutlass result file and find max throughput
            result_filename += '.gemm.csv'
            result_file = open(result_filename, 'r')
            lines = result_file.readlines()
            result_file.close()
            maxThroughput = 0.0
            min_time = 0.0
            for i in range(1, len(lines)): # skip the first line of headers
                tokens = lines[i].split(',')
                maxThroughput = max(maxThroughput, float(tokens[len(tokens) - 1]) / 1000) # last item is throughput
                min_time = min(min_time, float(tokens[len(tokens) - 3]))
            benchmarkResult.time_ms = min_time
            benchmarkResult.TFlops = maxThroughput
            resultRows.append(benchmarkResult.getResultRow())
            print(f'Max throughput: {maxThroughput} TFlops')
        else:
            cosmosdb.upsert_benchmark_results(resultRows, "cutlass", verboseLogs)
            cosmosdb.show_benchmark_summary("cutlass")
    else:
        for gemm in data:
            print(f"\nProcessing input: {gemm}")
            accera_gemm.benchmark_gemm(gemm, output_prefix, available_gpus, containerName, verboseLogs, compilerVersion, commit_id, commit_datetime, commit_branch, target_name, checkResult, deviceProperties)
        else:
            if containerName:
                cosmosdb.show_benchmark_summary(containerName)

def prepare_system_for_benchmark(target, available_gpus):
    deviceProperties = []
    compilerVersion = ''
    if target == 'AMD MI100':
        # fix shader clock speeds
        proc = subprocess.run(["rocm-smi", '--setsclk', '15'], capture_output=True, text=True)
        print(proc.stdout)

        proc = subprocess.run(["rocm-smi", '-g'], capture_output=True, text=True)
        print(proc.stdout)

        proc = subprocess.run(["hipcc", '--version'], capture_output=True, text=True)
        compilerVersion = proc.stdout
        print(compilerVersion)

        for deviceId in range(len(available_gpus)):
            proc = subprocess.run(["rocm-smi", '-a', '-d', str(deviceId), '--json'], capture_output=True, text=True)
            deviceProperties.append(proc.stdout)
    elif target == 'NVidia RTX A6000':
        # https://developer.download.nvidia.com/compute/DCGM/docs/nvidia-smi-367.38.pdf
        # enable persistence mode
        proc = subprocess.run(["nvidia-smi", '--persistence-mode=1'], capture_output=True, text=True)
        print(proc.stdout)

        # Run in exclusive process mode (1 process per GPU)
        proc = subprocess.run(["nvidia-smi", '--compute-mode=3'], capture_output=True, text=True)
        print(proc.stdout)

        # Set application clocks
        proc = subprocess.run(["nvidia-smi", '--applications-clocks=8001,2100'], capture_output=True, text=True)
        print(proc.stdout)

        proc = subprocess.run(["nvcc", '--version'], capture_output=True, text=True)
        compilerVersion = proc.stdout
        print(compilerVersion)

        for deviceId in range(len(available_gpus)):
            proc = subprocess.run(["nvidia-smi", '-q', '-i', str(deviceId)], capture_output=True, text=True)
            deviceProperties.append(proc.stdout)

    return deviceProperties, compilerVersion

def main(args=[]):
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--devices', help='The input config file (csv)', required=False, default="0,1,2,3")
    parser.add_argument('-i', '--input', help='The input config file (csv)', required=False)
    parser.add_argument('-b', '--branch', help='The git branch to use to tag the results to', required=False)
    parser.add_argument('-z', '--string', help='input config string (csv, semi-colon per row)', required=False)
    parser.add_argument('-t', '--target', help='The target the emitter is emitting HAT package for')
    parser.add_argument('-o', '--output', help='The output prefix', default="results")
    parser.add_argument('-roc', '--rocblas', help="The path to the rocblas_gemm tool", required=False)
    parser.add_argument('-cu', '--cublas', help="The path to the cublas_gemm tool", required=False)
    parser.add_argument('-ck', '--composable_kernel', help="The path to the composable-kernel tool", required=False)
    parser.add_argument('-cl', '--cutlass', help="The path to the cutlass tool", required=False)
    parser.add_argument('-u', '--upload', help="Specify the CosmosDB container name to upload the results to", required=False)
    parser.add_argument('-v', '--verbose', help="Enable verbose logging", required=False)
    parser.add_argument('-c', '--check', help="Verify correctness of the generated kernels", required=False)
    parser.add_argument('-j', '--janitor', help="Cleanup the output dir after running benchmark", required=False)

    args = parser.parse_args(args)

    if args.string and args.input:
        raise RuntimeError("input and string options are mutually exclusive")

    if args.rocblas and args.composable_kernel:
        raise RuntimeError("rocblas and composable_kernel options are mutually exclusive")

    f = None
    if args.string:
        args.string = ','.join(gemm_opts.CONFIG_HEADERS) + '\n' + '\n'.join(args.string.split(';'))
        f = StringIO(args.string)

    try:
        if f is None:
            f = open(args.input)

        reader = csv.DictReader(f, gemm_opts.CONFIG_HEADERS)
        gemm_inputs = [gemm_opts.GemmOpts(**data) for data in islice(reader, 1, None)]

    finally:
        f.close()

    available_gpus = []
    devices = args.devices.split(",")
    for dev in devices:
        dev_id = int(dev)
        while len(available_gpus) <= dev_id:
            available_gpus.append(False)
        else:
            available_gpus[dev_id] = True

    print(f"Running on devices: {args.devices}")

    print("Clean the output directory...")
    output_dir = os.path.split(args.output)[0] or '.'
    if os.path.isdir(output_dir):
        shutil.rmtree(output_dir)

    print(datetime.now())

    deviceProperties, compilerVersion = prepare_system_for_benchmark(args.target, available_gpus)

    benchmark_gemm_shapes(gemm_inputs, args.branch, args.target, args.output, args.rocblas, args.composable_kernel, args.cublas, args.cutlass, available_gpus, args.upload, args.verbose, args.check, compilerVersion, deviceProperties)

    print("Cleaning up output directory after benchmark")
    if args.janitor:
        shutil.rmtree(output_dir)

    print(datetime.now())


if __name__ == "__main__":
    main(sys.argv[1:])
