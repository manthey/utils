#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["psutil>=5.9"]
# ///
"""Cross-platform vmstat work-alike for Linux, macOS and Windows.

The columns mirror procps vmstat. Where a platform has no exact equivalent,
the closest available measure is used, as noted below. Columns with nothing
comparable on this OS (or for this user, e.g. cs on macOS without root) are
omitted; a value that is missing only for one report is shown as "-".

When stdout is a terminal, columns shrink to fit its width and values that no
longer fit get a decimal suffix (75458 -> 75k). Piped output is never
abbreviated unless COLUMNS is set.

FIELD DESCRIPTION FOR VM MODE
  Procs
    r      The number of runnable processes (running or waiting for run
           time). Linux: runnable tasks (/proc/stat). macOS as root: running
           plus runnable threads, summed over all tasks; macOS otherwise:
           1-minute load average, rounded. BSD: processes in the running
           state. Windows: processor queue length (ready threads).
    b      The number of processes blocked waiting for I/O to complete.
           Linux: tasks in uninterruptible sleep. BSD: disk-wait processes.
           Windows: disk queue length (outstanding I/O requests, not
           processes). macOS: unavailable.
  Memory (in --unit units)
    swpd   The amount of swap space used.
    free   The amount of idle memory. macOS: free pages excluding
           speculative ones (vm_stat "Pages free"). Windows: free + zeroed
           page lists.
    buff   The amount of memory used as buffers. Linux and BSD only.
    cache  The amount of memory used as cache. Linux: page cache plus
           reclaimable slab. macOS: file-backed plus purgeable pages
           (Activity Monitor's "Cached Files"). Windows: standby + modified
           page lists.
    inact  The amount of inactive memory (-a). Windows: standby list.
    active The amount of active memory (-a). Windows: memory in use by
           processes and the kernel.
  Swap (KiB/s)
    si     Memory swapped in from disk. macOS: compressor swap-ins.
           Windows: pages read to resolve hard faults.
    so     Memory swapped out to disk. macOS: compressor swap-outs.
           Windows: pages written to disk.
  IO (KiB/s)
    bi     Data received from block devices. Linux: pgpgin. Elsewhere: disk
           read volume.
    bo     Data sent to block devices. Linux: pgpgout. Elsewhere: disk write
           volume.
  System (per second)
    in     Interrupts, including the clock. macOS: unavailable.
    cs     Context switches. macOS: needs root (summed per-task counts).
  CPU (percentages of total CPU time)
    us     Time spent running non-kernel code (user time, including nice).
    sy     Time spent running kernel code (system time, including
           interrupt servicing).
    id     Time spent idle.
    wa     Time spent waiting for IO. Linux only.
    st     Time stolen from a virtual machine. Linux only, and only under a
           hypervisor (or once steal time has been seen).

EXTENDED FIELDS (-x)
  Kernel (in --unit units)
    wired  Memory that can never be paged out. macOS: wired pages.
           Windows: nonpaged pool.
  Commit (Linux and Windows)
    commit Memory promised to processes (Linux: Committed_AS; Windows:
           committed bytes), in --unit units.
    %lim   commit as a percentage of the commit limit. Windows refuses
           allocations at 100%; Linux exceeds it unless overcommit is strict.
  Compress (macOS compressor; Linux zswap)
    comp   Memory holding compressed pages, in --unit units.
    cmp    Memory compressed (KiB/s).
    dcmp   Memory decompressed (KiB/s).
  Events (per second)
    flt    Page faults, including ones resolved without disk I/O.
    sc     System calls. Windows only.
  Pressure
    lvl    Memory pressure level: norm, warn or crit. macOS only.

FIELD DESCRIPTION FOR DISK MODE (-d)
  Reads and Writes
    total   Operations completed successfully.
    merged  Adjacent requests grouped into one I/O. Linux only.
    sectors 512-byte sectors transferred.
    ms      Milliseconds spent on the operations.
  IO
    sec     Seconds spent doing I/O. Linux only.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import mmap
import os
import platform
import shutil
import sys
import time

import psutil

UNITS = {'k': 1000, 'K': 1024, 'm': 1000**2, 'M': 1024**2}
SUFFIXES = 'kMGTPE'
CUMULATIVE = (
    'swap_in',
    'swap_out',
    'block_in',
    'block_out',
    'interrupts',
    'ctx_switches',
    'forks',
    'compressions',
    'decompressions',
    'faults',
    'syscalls',
)
WINDOWS_COUNTERS = {
    'queue': r'\System\Processor Queue Length',
    'disk_queue': r'\PhysicalDisk(_Total)\Current Disk Queue Length',
    'free': r'\Memory\Free & Zero Page List Bytes',
    'standby_core': r'\Memory\Standby Cache Core Bytes',
    'standby_normal': r'\Memory\Standby Cache Normal Priority Bytes',
    'standby_reserve': r'\Memory\Standby Cache Reserve Bytes',
    'modified': r'\Memory\Modified Page List Bytes',
    'pages_in': r'\Memory\Pages Input/sec',
    'pages_out': r'\Memory\Pages Output/sec',
    'nonpaged_pool': r'\Memory\Pool Nonpaged Bytes',
    'committed': r'\Memory\Committed Bytes',
    'commit_limit': r'\Memory\Commit Limit',
    'faults': r'\Memory\Page Faults/sec',
    'syscalls': r'\System\System Calls/sec',
}
STANDBY_COUNTERS = ('standby_core', 'standby_normal', 'standby_reserve')
# kern.memorystatus_vm_pressure_level values (DISPATCH_MEMORYPRESSURE_*).
PRESSURE_LEVELS = {1: 'norm', 2: 'warn', 4: 'crit'}


@dataclasses.dataclass(frozen=True)
class Column:
    group: str
    name: str
    preferred: int
    minimum: int
    scalable: bool = True
    text: bool = False


@dataclasses.dataclass
class Sample:
    """One reading. Memory is in bytes, traffic counters in cumulative KiB."""

    time: float
    running: int | None
    blocked: int | None
    swap_used: int | None
    free: int | None
    buff: int | None
    cache: int | None
    active: int | None
    inactive: int | None
    swap_in: int | None
    swap_out: int | None
    block_in: int | None
    block_out: int | None
    interrupts: int | None
    ctx_switches: int | None
    forks: int | None
    cpu: dict
    wired: int | None = None
    compressed: int | None = None
    committed: int | None = None
    commit_limit: int | None = None
    compressions: int | None = None
    decompressions: int | None = None
    faults: int | None = None
    syscalls: int | None = None
    pressure: str | None = None


class PdhReader:
    """Reads raw Windows performance counters through pdh.dll."""

    def __init__(self, paths):
        import ctypes
        from ctypes import wintypes

        class RawCounter(ctypes.Structure):
            _fields_ = [
                ('status', wintypes.DWORD),
                ('timestamp', wintypes.FILETIME),
                ('first', ctypes.c_longlong),
                ('second', ctypes.c_longlong),
                ('multi_count', wintypes.DWORD),
            ]

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.raw_counter = RawCounter
        pdh = ctypes.WinDLL('pdh')
        handle_ptr = ctypes.POINTER(wintypes.HANDLE)
        pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, handle_ptr]
        pdh.PdhAddEnglishCounterW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCWSTR,
            ctypes.c_size_t,
            handle_ptr,
        ]
        pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
        pdh.PdhGetRawCounterValue.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(RawCounter),
        ]
        for function in (
            pdh.PdhOpenQueryW,
            pdh.PdhAddEnglishCounterW,
            pdh.PdhCollectQueryData,
            pdh.PdhGetRawCounterValue,
        ):
            function.restype = ctypes.c_ulong
        self.pdh = pdh
        self.query = wintypes.HANDLE()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query)):
            msg = 'PdhOpenQueryW failed'
            raise OSError(msg)
        self.handles = {}
        for name, path in paths.items():
            handle = wintypes.HANDLE()
            if not pdh.PdhAddEnglishCounterW(self.query, path, 0, ctypes.byref(handle)):
                self.handles[name] = handle

    def read(self):
        if self.pdh.PdhCollectQueryData(self.query):
            return {}
        values = {}
        for name, handle in self.handles.items():
            raw = self.raw_counter()
            kind = self.wintypes.DWORD()
            status = self.pdh.PdhGetRawCounterValue(
                handle, self.ctypes.byref(kind), self.ctypes.byref(raw),
            )
            if status == 0 and raw.status in (0, 1):
                values[name] = raw.first
        return values


HOST_VM_INFO64 = 4
PROC_PIDTASKINFO = 4


class MacStats:
    """Reads Mach VM statistics and per-task counters through libSystem."""

    def __init__(self):
        import ctypes

        def fields(kind, *names):
            return [(name, kind) for name in names]

        u32, u64, i32 = ctypes.c_uint32, ctypes.c_uint64, ctypes.c_int32

        class VmStatistics64(ctypes.Structure):
            _fields_ = (
                fields(u32, 'free_count', 'active_count', 'inactive_count', 'wire_count') +
                fields(u64, 'zero_fill_count', 'reactivations', 'pageins', 'pageouts',
                       'faults', 'cow_faults', 'lookups', 'hits', 'purges') +
                fields(u32, 'purgeable_count', 'speculative_count') +
                fields(u64, 'decompressions', 'compressions', 'swapins', 'swapouts') +
                fields(u32, 'compressor_page_count', 'throttled_count',
                       'external_page_count', 'internal_page_count') +
                fields(u64, 'total_uncompressed_pages_in_compressor')
            )

        class ProcTaskInfo(ctypes.Structure):
            _fields_ = (
                fields(u64, 'virtual_size', 'resident_size', 'total_user', 'total_system',
                       'threads_user', 'threads_system') +
                fields(i32, 'policy', 'faults', 'pageins', 'cow_faults', 'messages_sent',
                       'messages_received', 'syscalls_mach', 'syscalls_unix', 'csw',
                       'threadnum', 'numrunning', 'priority')
            )

        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        libc.mach_host_self.restype = u32
        libc.host_page_size.argtypes = [u32, ctypes.POINTER(ctypes.c_size_t)]
        libc.host_statistics64.argtypes = [u32, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(u32)]
        libc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        libc.proc_pidinfo.argtypes = [ctypes.c_int,
                                      ctypes.c_int, u64, ctypes.c_void_p, ctypes.c_int]
        libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
                                      ctypes.c_size_t]
        self.ctypes = ctypes
        self.libc = libc
        self.vm_type = VmStatistics64
        self.task_type = ProcTaskInfo
        self.host = libc.mach_host_self()
        page_size = ctypes.c_size_t()
        if libc.host_page_size(self.host, ctypes.byref(page_size)):
            msg = 'host_page_size failed'
            raise OSError(msg)
        self.page_size = page_size.value
        self.privileged = os.geteuid() == 0
        self.csw_by_pid = {}
        self.csw_total = 0

    def pages_to_kib(self, pages):
        return pages * self.page_size // 1024

    def pressure_level(self):
        ctypes = self.ctypes
        level = ctypes.c_int()
        size = ctypes.c_size_t(ctypes.sizeof(level))
        if self.libc.sysctlbyname(b'kern.memorystatus_vm_pressure_level',
                                  ctypes.byref(level), ctypes.byref(size), None, 0):
            return None
        return PRESSURE_LEVELS.get(level.value, str(level.value))

    def vm_statistics(self):
        stats = self.vm_type()
        count = self.ctypes.c_uint32(self.ctypes.sizeof(stats) // 4)
        status = self.libc.host_statistics64(
            self.host, HOST_VM_INFO64, self.ctypes.byref(stats), self.ctypes.byref(count),
        )
        return None if status else stats

    def task_totals(self):
        """(running + runnable threads, cumulative context switches); needs root."""
        if not self.privileged:
            return None, None
        ctypes = self.ctypes
        capacity = max(self.libc.proc_listallpids(None, 0), 0) + 256
        pids = (ctypes.c_int * capacity)()
        count = self.libc.proc_listallpids(pids, ctypes.sizeof(pids))
        if count <= 0:
            return None, None
        info = self.task_type()
        size = ctypes.sizeof(info)
        running = 0
        seen = {}
        for pid in pids[:count]:
            if self.libc.proc_pidinfo(pid, PROC_PIDTASKINFO, 0, ctypes.byref(info), size) != size:
                continue
            # pti_csw is a 32-bit count; a drop means the pid was reused or
            # the counter wrapped.
            csw = info.csw & 0xFFFFFFFF
            previous = self.csw_by_pid.get(pid)
            if previous is not None and csw >= previous:
                self.csw_total += csw - previous
            else:
                self.csw_total += csw
            seen[pid] = csw
            running += info.numrunning
        self.csw_by_pid = seen
        return running, self.csw_total


def read_proc(path):
    """Parse 'key value ...' lines from a /proc file, keeping the first number."""
    values = {}
    try:
        with open(path) as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    values[parts[0]] = int(parts[1])
    except OSError:
        pass
    return values


def disk_traffic():
    try:
        counters = psutil.disk_io_counters()
    except (OSError, RuntimeError):
        counters = None
    if counters is None:
        return None, None
    return counters.read_bytes // 1024, counters.write_bytes // 1024


def cpu_buckets(system):
    """Cumulative CPU seconds grouped the way vmstat groups them."""
    times = psutil.cpu_times()._asdict()
    if system == 'Windows':
        # psutil's system time already contains interrupt and DPC time.
        times.pop('interrupt', None)
        times.pop('dpc', None)
    total = sum(value for key, value in times.items() if key not in ('guest', 'guest_nice'))
    return {
        'us': times.get('user', 0.0) + times.get('nice', 0.0),
        'sy': times.get('system', 0.0) + times.get('irq', 0.0) + times.get('softirq', 0.0),
        'id': times.get('idle', 0.0),
        'wa': times.get('iowait'),
        'st': times.get('steal'),
        'total': total,
    }


def steal_possible():
    """Whether steal time can be nonzero: under a hypervisor, or already seen."""
    try:
        with open('/proc/cpuinfo') as handle:
            for line in handle:
                if line.startswith('flags') and 'hypervisor' in line.split():
                    return True
    except OSError:
        pass
    if os.path.exists('/sys/hypervisor/type'):
        return True
    return getattr(psutil.cpu_times(), 'steal', 0) > 0


class Sampler:
    def __init__(self):
        self.system = platform.system()
        self.page_kib = mmap.PAGESIZE / 1024
        self.has_steal = self.system == 'Linux' and steal_possible()
        self.pdh = None
        self.mac = None
        if self.system == 'Darwin':
            try:
                self.mac = MacStats()
            except (OSError, AttributeError):
                self.mac = None
        if self.system == 'Windows':
            try:
                self.pdh = PdhReader(WINDOWS_COUNTERS)
            except OSError:
                self.pdh = None

    def pages_to_kib(self, pages):
        return None if pages is None else int(pages * self.page_kib)

    def process_counts(self, pdh):
        if self.system == 'Windows':
            return pdh.get('queue'), pdh.get('disk_queue')
        running = blocked = 0
        for proc in psutil.process_iter(['status']):
            status = proc.info.get('status')
            if status == psutil.STATUS_RUNNING:
                running += 1
            elif status == psutil.STATUS_DISK_SLEEP:
                blocked += 1
        return running, blocked

    def memory_breakdown(self, vm, pdh, mac_vm):
        """Return (free, buff, cache, active, inactive) in bytes."""
        if self.system == 'Linux':
            return vm.free, vm.buffers, vm.cached, vm.active, vm.inactive
        if self.system == 'Darwin':
            if mac_vm is None:
                return vm.free, None, vm.inactive, vm.active, vm.inactive
            page = self.mac.page_size
            return (
                max(mac_vm.free_count - mac_vm.speculative_count, 0) * page,
                None,
                (mac_vm.external_page_count + mac_vm.purgeable_count) * page,
                mac_vm.active_count * page,
                mac_vm.inactive_count * page,
            )
        if self.system == 'Windows':
            free = pdh.get('free', vm.available)
            standby_parts = [pdh[key] for key in STANDBY_COUNTERS if key in pdh]
            if not standby_parts:
                return free, None, None, None, None
            standby = sum(standby_parts)
            modified = pdh.get('modified', 0)
            in_use = max(vm.total - free - standby - modified, 0)
            return free, None, standby + modified, in_use, standby
        return (
            vm.free,
            getattr(vm, 'buffers', None),
            getattr(vm, 'cached', None),
            getattr(vm, 'active', None),
            getattr(vm, 'inactive', None),
        )

    def extended(self, pdh, mac_vm):
        """Fields for --extended; memory in bytes, traffic in cumulative KiB."""
        if self.system == 'Linux':
            meminfo = read_proc('/proc/meminfo')
            vmstat = read_proc('/proc/vmstat')

            def size(key):
                return None if key not in meminfo else meminfo[key] * 1024

            return {
                'compressed': size('Zswap:'),
                'committed': size('Committed_AS:'),
                'commit_limit': size('CommitLimit:'),
                'compressions': self.pages_to_kib(vmstat.get('zswpout')),
                'decompressions': self.pages_to_kib(vmstat.get('zswpin')),
                'faults': vmstat.get('pgfault'),
            }
        if self.system == 'Darwin':
            if self.mac is None:
                return {}
            fields = {'pressure': self.mac.pressure_level()}
            if mac_vm is not None:
                fields.update(
                    wired=mac_vm.wire_count * self.mac.page_size,
                    compressed=mac_vm.compressor_page_count * self.mac.page_size,
                    compressions=self.mac.pages_to_kib(mac_vm.compressions),
                    decompressions=self.mac.pages_to_kib(mac_vm.decompressions),
                    faults=mac_vm.faults,
                )
            return fields
        if self.system == 'Windows':
            return {
                'wired': pdh.get('nonpaged_pool'),
                'committed': pdh.get('committed'),
                'commit_limit': pdh.get('commit_limit'),
                'faults': pdh.get('faults'),
                'syscalls': pdh.get('syscalls'),
            }
        return {}

    def sample(self):
        now = time.time()
        pdh = self.pdh.read() if self.pdh else {}
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        stats = psutil.cpu_stats()
        forks = None
        mac_vm = None
        interrupts, ctx_switches = stats.interrupts, stats.ctx_switches
        if self.system == 'Linux':
            stat = read_proc('/proc/stat')
            vmstat = read_proc('/proc/vmstat')
            running, blocked = stat.get('procs_running'), stat.get('procs_blocked')
            forks = stat.get('processes')
            swap_in = self.pages_to_kib(vmstat.get('pswpin'))
            swap_out = self.pages_to_kib(vmstat.get('pswpout'))
            block_in, block_out = vmstat.get('pgpgin'), vmstat.get('pgpgout')
        elif self.system == 'Darwin':
            # psutil's macOS interrupt and context-switch counters are not
            # usable.
            interrupts = ctx_switches = None
            threads = None
            swap_in = swap_out = None
            if self.mac:
                mac_vm = self.mac.vm_statistics()
                threads, ctx_switches = self.mac.task_totals()
            if mac_vm is not None:
                swap_in = self.mac.pages_to_kib(mac_vm.swapins)
                swap_out = self.mac.pages_to_kib(mac_vm.swapouts)
            running = round(psutil.getloadavg()[0]) if threads is None else threads
            blocked = None
            block_in, block_out = disk_traffic()
        else:
            running, blocked = self.process_counts(pdh)
            if self.system == 'Windows':
                swap_in = self.pages_to_kib(pdh.get('pages_in'))
                swap_out = self.pages_to_kib(pdh.get('pages_out'))
            else:
                swap_in, swap_out = swap.sin // 1024, swap.sout // 1024
            block_in, block_out = disk_traffic()
        free, buff, cache, active, inactive = self.memory_breakdown(vm, pdh, mac_vm)
        cpu = cpu_buckets(self.system)
        if not self.has_steal:
            cpu['st'] = None
        return Sample(
            time=now,
            running=running,
            blocked=blocked,
            swap_used=swap.used,
            free=free,
            buff=buff,
            cache=cache,
            active=active,
            inactive=inactive,
            swap_in=swap_in,
            swap_out=swap_out,
            block_in=block_in,
            block_out=block_out,
            interrupts=interrupts,
            ctx_switches=ctx_switches,
            forks=forks,
            cpu=cpu,
            **self.extended(pdh, mac_vm),
        )

    def boot_baseline(self, sample):
        """A zeroed sample at boot time, so the first report is since-boot averages."""
        zeroed = {name: (None if getattr(sample, name) is None else 0) for name in CUMULATIVE}
        cpu = {key: (None if value is None else 0.0) for key, value in sample.cpu.items()}
        return dataclasses.replace(sample, time=psutil.boot_time(), cpu=cpu, **zeroed)


def compact(number, width):
    """Shorten an integer with a decimal suffix (75458 -> 75k) so it fits width."""
    for power, suffix in enumerate(SUFFIXES, start=1):
        scaled = number / 1000**power
        if f'{scaled:.0f}' == '0':
            break
        for decimals in (2, 1, 0):
            digits = f'{scaled:.{decimals}f}'
            if float(digits) >= 1000:
                break
            text = digits + suffix
            if len(text) <= width:
                return text
    return str(number)


def plain(value):
    return '-' if value is None else str(value)


def format_cell(value, width, column, abbreviate):
    text = plain(value)
    if column.text:
        return text[:width].ljust(width)
    if len(text) > width and abbreviate and column.scalable and value is not None:
        text = compact(value, width)
    return text.rjust(width)


def layout(columns, rows, available):
    """Pick column widths: natural size first, then shrink the widest to fit."""
    widths = []
    for index, column in enumerate(columns):
        natural = max((len(plain(row[index])) for row in rows), default=1)
        widths.append(max(column.preferred, column.minimum, len(column.name), natural))
    if available is None:
        return widths
    natural_widths = list(widths)
    budget = available - (len(columns) - 1)

    def floor(index, use_preferred):
        column = columns[index]
        if not column.scalable:
            return natural_widths[index]
        lowest = max(column.minimum, len(column.name))
        return max(lowest, column.preferred) if use_preferred else lowest

    for use_preferred in (True, False):
        while sum(widths) > budget:
            shrinkable = [i for i, width in enumerate(widths) if width > floor(i, use_preferred)]
            if not shrinkable:
                break
            widest = max(shrinkable, key=lambda i: widths[i])
            widths[widest] -= 1
    return widths


def supported(columns, rows):
    """Indices of columns that have a value in some row (all of them if there are no rows)."""
    if not rows:
        return list(range(len(columns)))
    return [i for i in range(len(columns)) if any(row[i] is not None for row in rows)]


def pick(items, indices):
    return [items[i] for i in indices]


def render_header(columns, widths):
    groups = []
    for column, width in zip(columns, widths, strict=True):
        if groups and groups[-1][0] == column.group:
            groups[-1][1] += width + 1
        else:
            groups.append([column.group, width])
    top = ' '.join(name[:span].center(span, '-') for name, span in groups)
    bottom = ' '.join(
        column.name.ljust(width) if column.text else column.name.rjust(width)
        for column, width in zip(columns, widths, strict=True)
    )
    return f'{top}\n{bottom}'


def render_row(columns, widths, values, abbreviate):
    return ' '.join(
        format_cell(value, width, column, abbreviate)
        for column, width, value in zip(columns, widths, values, strict=True)
    )


def terminal_size():
    """(columns, lines); columns is None for unbounded piped output."""
    if sys.stdout.isatty():
        size = shutil.get_terminal_size()
        return size.columns, size.lines
    columns = os.environ.get('COLUMNS', '')
    return (int(columns) if columns.isdigit() else None), None


def vm_columns(wide, active, timestamp, extended):
    memory = 12 if wide else 6
    io = 6 if wide else 5
    system = 6 if wide else 4
    procs = 4 if wide else 2
    cpu = 3 if wide else 2
    columns = [
        Column('procs', 'r', procs, 2),
        Column('procs', 'b', procs, 2),
        Column('memory', 'swpd', memory, 4),
        Column('memory', 'free', memory, 4),
        Column('memory', 'inact' if active else 'buff', memory, 4),
        Column('memory', 'active' if active else 'cache', memory, 4),
        Column('swap', 'si', 4, 4),
        Column('swap', 'so', 4, 4),
        Column('io', 'bi', io, 4),
        Column('io', 'bo', io, 4),
        Column('system', 'in', system, 4),
        Column('system', 'cs', system, 4),
    ]
    columns += [Column('cpu', name, cpu, 2, scalable=False)
                for name in ('us', 'sy', 'id', 'wa', 'st')]
    if extended:
        columns += [
            Column('kernel', 'wired', memory, 4),
            Column('commit', 'commit', memory, 4),
            Column('commit', '%lim', 4, 4, scalable=False),
            Column('compress', 'comp', memory, 4),
            Column('compress', 'cmp', 4, 4),
            Column('compress', 'dcmp', 4, 4),
            Column('events', 'flt', system, 4),
            Column('events', 'sc', system, 4),
            Column('pressure', 'lvl', 4, 4, scalable=False, text=True),
        ]
    if timestamp:
        zone = datetime.datetime.now().astimezone().tzname() or ''
        columns.append(Column('timestamp', zone, 19, 19, scalable=False, text=True))
    return columns


def vm_values(prev, cur, active, unit, extended):
    elapsed = max(cur.time - prev.time, 1e-6)

    def rate(name):
        now, before = getattr(cur, name), getattr(prev, name)
        if now is None or before is None:
            return None
        return round(max(now - before, 0) / elapsed)

    def scaled(value):
        return None if value is None else value // unit

    cpu_total = cur.cpu['total'] - prev.cpu['total']

    def percent(key):
        if cur.cpu[key] is None:
            return None
        if cpu_total <= 0:
            return 0
        return round(100 * max(cur.cpu[key] - prev.cpu[key], 0) / cpu_total)

    memory = (cur.inactive, cur.active) if active else (cur.buff, cur.cache)
    values = [
        cur.running,
        cur.blocked,
        scaled(cur.swap_used),
        scaled(cur.free),
        scaled(memory[0]),
        scaled(memory[1]),
        rate('swap_in'),
        rate('swap_out'),
        rate('block_in'),
        rate('block_out'),
        rate('interrupts'),
        rate('ctx_switches'),
        percent('us'),
        percent('sy'),
        percent('id'),
        percent('wa'),
        percent('st'),
    ]
    if extended:
        limit = None
        if cur.committed is not None and cur.commit_limit:
            limit = round(100 * cur.committed / cur.commit_limit)
        values += [
            scaled(cur.wired),
            scaled(cur.committed),
            limit,
            scaled(cur.compressed),
            rate('compressions'),
            rate('decompressions'),
            rate('faults'),
            rate('syscalls'),
            cur.pressure,
        ]
    return values


def wait_until(deadline):
    time.sleep(max(0.0, deadline - time.monotonic()))


def run_vm(args, sampler):  # noqa
    columns = vm_columns(args.wide, args.active, args.timestamp, args.extended)
    unit = UNITS[args.unit]
    cur = sampler.sample()
    prev = sampler.boot_baseline(cur)
    if args.no_first:
        time.sleep(args.delay)
        prev, cur = cur, sampler.sample()
    widths = shown_size = keep = None
    since_header = lines = 0
    deadline = time.monotonic()
    while True:
        values = vm_values(prev, cur, args.active, unit, args.extended)
        if args.timestamp:
            values.append(time.strftime('%Y-%m-%d %H:%M:%S'))
        if keep is None:
            # Drop columns this OS (or this user's privileges) can never fill.
            keep = supported(columns, [values])
            columns = pick(columns, keep)
        values = pick(values, keep)
        size = terminal_size()
        available, height = size

        if widths is None:
            # Compute natural column widths.
            natural_widths = []
            for idx, col in enumerate(columns):
                max_data = max((len(plain(values[idx]))), 1)
                if col.scalable:
                    natural = max(col.preferred, col.minimum, len(col.name), max_data)
                else:
                    natural = max(col.minimum + len(col.name), max_data)
                natural_widths.append(natural)
            total_needed = sum(natural_widths) + len(columns) - 1
            if available is not None and total_needed <= available:
                # Data fits; lock these natural widths, no layout needed
                candidate = natural_widths
            else:
                candidate = layout(columns, [values], available)
        elif size != shown_size:
            # Terminal resized.  Check if natural widths now fit.
            natural_widths = []
            for idx, col in enumerate(columns):
                max_data = max((len(plain(values[idx]))), 1)
                if col.scalable:
                    natural = max(col.preferred, col.minimum, len(col.name), max_data)
                else:
                    natural = max(col.minimum + len(col.name), max_data)
                natural_widths.append(natural)
            total_needed = sum(natural_widths) + len(columns) - 1
            if available is not None and total_needed <= available:
                candidate = natural_widths
            else:
                candidate = layout(columns, [values], available)
        else:
            # No resize. Don't re-layout on every data change
            candidate = list(widths)
        page_full = height is not None and since_header >= max(height - 2, 1)
        if widths is None or (not args.one_header and (
            size != shown_size or
            any(new > old for new, old in zip(candidate, widths, strict=True)) or
            page_full
        )):
            widths, shown_size = candidate, size
            print(render_header(columns, widths))
            since_header = 0
        print(render_row(columns, widths, values, available is not None), flush=True)
        since_header += 1
        lines += 1
        if args.delay is None or (args.count is not None and lines >= args.count):
            return
        deadline += args.delay
        wait_until(deadline)
        prev, cur = cur, sampler.sample()


def disk_columns():
    columns = [Column('disk', '', 6, 4, scalable=False, text=True)]
    for group in ('reads', 'writes'):
        columns += [
            Column(group, 'total', 6, 4),
            Column(group, 'merged', 6, 4),
            Column(group, 'sectors', 7, 4),
            Column(group, 'ms', 7, 4),
        ]
    columns += [Column('IO', 'cur', 6, 3), Column('IO', 'sec', 6, 3)]
    return columns


def disk_rows():
    try:
        counters = psutil.disk_io_counters(perdisk=True) or {}
    except (OSError, RuntimeError):
        counters = {}
    rows = []
    for name, disk in sorted(counters.items()):
        busy = getattr(disk, 'busy_time', None)
        rows.append([
            name,
            disk.read_count,
            getattr(disk, 'read_merged_count', None),
            disk.read_bytes // 512,
            getattr(disk, 'read_time', None),
            disk.write_count,
            getattr(disk, 'write_merged_count', None),
            disk.write_bytes // 512,
            getattr(disk, 'write_time', None),
            None,
            None if busy is None else busy // 1000,
        ])
    return rows


def run_disk(args):
    columns = disk_columns()
    keep = None
    lines = 0
    deadline = time.monotonic()
    while True:
        rows = disk_rows()
        if keep is None and rows:
            keep = supported(columns, rows)
            columns = pick(columns, keep)
        if keep is not None:
            rows = [pick(row, keep) for row in rows]
        available, _ = terminal_size()
        widths = layout(columns, rows or [[None] * len(columns)], available)
        print(render_header(columns, widths))
        for row in rows:
            print(render_row(columns, widths, row, available is not None))
        sys.stdout.flush()
        lines += 1
        if args.delay is None or (args.count is not None and lines >= args.count):
            return
        deadline += args.delay
        wait_until(deadline)


def print_stats(args, sampler):
    unit = UNITS[args.unit]
    cur = sampler.sample()
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    times = psutil.cpu_times()._asdict()

    def memory(value, label):
        return (None if value is None else value // unit, f'{args.unit} {label}')

    entries = [
        memory(vm.total, 'total memory'),
        memory(vm.used, 'used memory'),
        memory(cur.active, 'active memory'),
        memory(cur.inactive, 'inactive memory'),
        memory(cur.free, 'free memory'),
        memory(cur.buff, 'buffer memory'),
        memory(cur.cache, 'swap cache'),
        memory(swap.total, 'total swap'),
        memory(swap.used, 'used swap'),
        memory(swap.free, 'free swap'),
        memory(cur.wired, 'wired memory'),
        memory(cur.committed, 'committed memory'),
        memory(cur.commit_limit, 'commit limit'),
        memory(cur.compressed, 'compressed memory'),
    ]
    tick_labels = [
        ('user', 'non-nice user cpu ticks'),
        ('nice', 'nice user cpu ticks'),
        ('system', 'system cpu ticks'),
        ('idle', 'idle cpu ticks'),
        ('iowait', 'IO-wait cpu ticks'),
        ('irq', 'IRQ cpu ticks'),
        ('interrupt', 'IRQ cpu ticks'),
        ('softirq', 'softirq cpu ticks'),
        ('dpc', 'softirq cpu ticks'),
        ('steal', 'stolen cpu ticks'),
    ]
    if not sampler.has_steal:
        times.pop('steal', None)
    entries += [(round(times[key] * 100), label) for key, label in tick_labels if key in times]

    def kib_to_pages(value):
        return None if value is None else int(value / sampler.page_kib)

    entries += [
        (cur.block_in, 'pages paged in'),
        (cur.block_out, 'pages paged out'),
        (kib_to_pages(cur.swap_in), 'pages swapped in'),
        (kib_to_pages(cur.swap_out), 'pages swapped out'),
        (cur.interrupts, 'interrupts'),
        (cur.ctx_switches, 'CPU context switches'),
        (int(psutil.boot_time()), 'boot time'),
        (cur.forks, 'forks'),
        (kib_to_pages(cur.compressions), 'pages compressed'),
        (kib_to_pages(cur.decompressions), 'pages decompressed'),
        (cur.faults, 'page faults'),
        (cur.syscalls, 'system calls'),
        (cur.pressure, 'memory pressure level'),
    ]
    for value, label in entries:
        if value is not None:
            print(f'{value:>13} {label}')


def positive_number(kind):
    def parse(text):
        try:
            value = kind(text)
        except ValueError:
            msg = f'invalid number: {text!r}'
            raise argparse.ArgumentTypeError(msg) from None
        if value <= 0:
            msg = 'must be greater than 0'
            raise argparse.ArgumentTypeError(msg)
        return value

    return parse


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='vmstat',
        description='Report virtual memory statistics (cross-platform vmstat work-alike).',
        epilog='Without a delay, one report of averages since boot is printed.\n\n' +
        (__doc__ or '').partition('\n')[2].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('delay', nargs='?', type=positive_number(float),
                        help='seconds between updates')
    parser.add_argument('count', nargs='?', type=positive_number(int),
                        help='number of updates (default: forever when a delay is given)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('-d', '--disk', action='store_true', help='disk statistics')
    mode.add_argument('-s', '--stats', action='store_true', help='event counter statistics')
    parser.add_argument('-a', '--active', action='store_true',
                        help='show active/inactive memory instead of buff/cache')
    parser.add_argument('-n', '--one-header', action='store_true',
                        help='print the header only once')
    parser.add_argument('-S', '--unit', choices=UNITS, default='K',
                        help='memory unit: k=1000, K=1024, m=1000000, M=1048576 bytes')
    parser.add_argument('-t', '--timestamp', action='store_true', help='append a timestamp')
    parser.add_argument('-w', '--wide', action='store_true', help='wide output')
    parser.add_argument('-x', '--extended', action='store_true',
                        help='add platform-specific columns (see EXTENDED FIELDS)')
    parser.add_argument('-y', '--no-first', action='store_true',
                        help='skip the first report (averages since boot)')
    args = parser.parse_args(argv)
    if args.no_first and args.delay is None:
        args.delay, args.count = 1.0, 1
    return args


def main(argv=None):
    args = parse_args(argv)
    sampler = Sampler()
    try:
        if args.stats:
            print_stats(args, sampler)
        elif args.disk:
            run_disk(args)
        else:
            run_vm(args, sampler)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
