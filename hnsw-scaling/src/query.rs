use super::{new_index, vectors, Result, DIM, K};
use std::fs::{self, File, OpenOptions};
use std::hint::black_box;
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::sync::{Barrier, OnceLock};
use std::thread;
use std::time::{Duration, Instant};
use usearch::Index;

struct Perf {
    control: File,
    ack: BufReader<File>,
}

impl Perf {
    fn open() -> Result<Vec<Self>> {
        let spec = std::env::var("HNSW_PERF_FIFOS").unwrap_or_default();
        spec.split_whitespace()
            .map(|pair| {
                let (control, ack) = pair.split_once(',').ok_or("expected CONTROL,ACK")?;
                Ok(Self {
                    control: OpenOptions::new().write(true).open(control)?,
                    ack: BufReader::new(File::open(ack)?),
                })
            })
            .collect()
    }

    fn command(&mut self, command: &str) -> Result<()> {
        writeln!(self.control, "{command}")?;
        let mut line = String::new();
        self.ack.read_line(&mut line)?;
        if line.trim_matches(|c: char| c == '\0' || c.is_ascii_whitespace()) != "ack" {
            return Err(format!("unexpected perf reply {line:?}").into());
        }
        Ok(())
    }
}

fn pin(cpu: usize) -> std::io::Result<()> {
    // SAFETY: the CPU set is initialized before it is passed to the kernel.
    unsafe {
        let mut set: libc::cpu_set_t = std::mem::zeroed();
        libc::CPU_SET(cpu, &mut set);
        if libc::sched_setaffinity(0, size_of::<libc::cpu_set_t>(), &set) != 0 {
            return Err(std::io::Error::last_os_error());
        }
    }
    Ok(())
}

fn allowed_cpus() -> std::io::Result<Vec<usize>> {
    // SAFETY: sched_getaffinity writes into the initialized CPU set.
    unsafe {
        let mut set: libc::cpu_set_t = std::mem::zeroed();
        if libc::sched_getaffinity(0, size_of::<libc::cpu_set_t>(), &mut set) != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok((0..libc::CPU_SETSIZE as usize)
            .filter(|&cpu| libc::CPU_ISSET(cpu, &set))
            .collect())
    }
}

fn usage() -> libc::rusage {
    // SAFETY: getrusage writes into valid storage and uses the calling thread.
    unsafe {
        let mut usage = std::mem::zeroed();
        assert_eq!(libc::getrusage(libc::RUSAGE_THREAD, &mut usage), 0);
        usage
    }
}

fn cpu_seconds(usage: &libc::rusage) -> f64 {
    (usage.ru_utime.tv_sec + usage.ru_stime.tv_sec) as f64
        + (usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) as f64 / 1e6
}

fn open_view(path: &str, ef: usize, slots: usize) -> Result<Index> {
    let index = new_index(ef)?;
    index.reserve_capacity_and_threads(0, slots)?;
    index.view(path)?;
    assert_eq!(index.expansion_search(), ef);
    Ok(index)
}

fn query_count(path: &Path) -> Result<usize> {
    let bytes_per_query = (DIM * size_of::<f32>()) as u64;
    let bytes = path.metadata()?.len();
    if bytes == 0 || bytes % bytes_per_query != 0 {
        return Err("query file must contain one or more complete float32 vectors".into());
    }
    Ok(usize::try_from(bytes / bytes_per_query)?)
}

fn prefault(path: &Path, expected_maps: usize) -> Result<(u64, u64)> {
    let name = path.to_str().ok_or("invalid index path")?;
    let mut maps = 0;
    for line in fs::read_to_string("/proc/self/maps")?
        .lines()
        .filter(|line| line.ends_with(name))
    {
        let (lo, hi) = line
            .split_whitespace()
            .next()
            .unwrap()
            .split_once('-')
            .unwrap();
        let lo = usize::from_str_radix(lo, 16)?;
        let hi = usize::from_str_radix(hi, 16)?;
        // SAFETY: the index mappings remain live for this entire call.
        if unsafe { libc::madvise(lo as *mut libc::c_void, hi - lo, libc::MADV_POPULATE_READ) } != 0
        {
            return Err(std::io::Error::last_os_error().into());
        }
        maps += 1;
    }
    assert_eq!(maps, expected_maps);
    mapped_pages(path)
}

fn mapped_pages(path: &Path) -> Result<(u64, u64)> {
    let path = path.to_str().ok_or("invalid index path")?;
    let mut pages = [0; 2];
    for line in fs::read_to_string("/proc/self/numa_maps")?
        .lines()
        .filter(|line| line.contains(path))
    {
        for token in line.split_whitespace() {
            for (node, total) in pages.iter_mut().enumerate() {
                if let Some(count) = token.strip_prefix(&format!("N{node}=")) {
                    *total += count.parse::<u64>()?;
                }
            }
        }
    }
    assert!(pages.iter().sum::<u64>() > 0);
    Ok((pages[0], pages[1]))
}

fn recall(index: &Index, queries: &[f32], truth: &str) -> Result<()> {
    let gt = fs::read_to_string(truth)?;
    let mut hits = 0;
    let mut count = 0;
    for (query, line) in queries.chunks_exact(DIM).zip(gt.lines()) {
        let keys = line
            .split(',')
            .map(str::parse::<u64>)
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let result = index.search(query, K)?;
        assert_eq!(keys.len(), K);
        assert_eq!(result.keys.len(), K);
        hits += result.keys.iter().filter(|key| keys.contains(key)).count();
        count += 1;
    }
    assert_eq!(count, queries.len() / DIM);
    assert_eq!(count, gt.lines().count());
    println!(
        "queries={count},recall_at_10={:.6},kernel={}",
        hits as f64 / (count * K) as f64,
        index.hardware_acceleration()
    );
    Ok(())
}

#[cfg(feature = "diagnostic")]
fn graph_work(index: &Index, queries: &[f32]) -> Result<()> {
    let mut visited = 0;
    let mut distances = 0;
    for query in queries.chunks_exact(DIM) {
        let pooled = index.search(query, K)?;
        // SAFETY: this loop is serial and no other code accesses slot 0.
        let explicit = unsafe { index.search_f32_on_slot(query, K, 0)? };
        assert_eq!(pooled.keys, explicit.keys);
        assert_eq!(pooled.distances, explicit.distances);
        assert_eq!(pooled.visited, explicit.visited);
        assert_eq!(pooled.computed, explicit.computed);
        visited += explicit.visited;
        distances += explicit.computed;
    }
    let count = queries.len() / DIM;
    println!(
        "queries={count},visited_per_query={:.3},distances_per_query={:.3}",
        visited as f64 / count as f64,
        distances as f64 / count as f64
    );
    Ok(())
}

#[derive(Default)]
struct Worker {
    good: u64,
    failed: u64,
    short: u64,
    cpu: f64,
    minor: i64,
    major: i64,
    involuntary: i64,
    voluntary: i64,
    latency_ns: Vec<u64>,
    sampled_nodes: [u64; 2],
    #[cfg(feature = "slot-timing")]
    slot_wait: Vec<(u64, u64, u64)>,
}

const CPU_GROUPS: usize = libc::CPU_SETSIZE as usize;

fn cpu_groups() -> Result<[usize; CPU_GROUPS]> {
    let mut groups = [0; CPU_GROUPS];
    for node in 0..2 {
        let path = format!("/sys/devices/system/node/node{node}/cpulist");
        let list = match fs::read_to_string(path) {
            Ok(list) => list,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => continue,
            Err(err) => return Err(err.into()),
        };
        for range in list.trim().split(',').filter(|range| !range.is_empty()) {
            let (first, last) = range.split_once('-').unwrap_or((range, range));
            let first = first.parse::<usize>()?;
            let last = last.parse::<usize>()?;
            if let Some(cpus) = groups.get_mut(first..=last) {
                cpus.fill(node + 1);
            }
        }
    }
    Ok(groups)
}

fn host_ticks(cpu_groups: &[usize; CPU_GROUPS], cpus: &[usize]) -> Result<[(u64, u64); 4]> {
    let stat = fs::read_to_string("/proc/stat")?;
    let mut groups = [(0, 0); 4];
    for line in stat.lines().take_while(|line| line.starts_with("cpu")) {
        let mut fields = line.split_whitespace();
        let id = fields.next().unwrap().strip_prefix("cpu").unwrap();
        let ticks = fields
            .take(8)
            .map(str::parse::<u64>)
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let total = ticks.iter().sum::<u64>();
        let idle = ticks[3] + ticks[4];
        if id.is_empty() {
            groups[0] = (total, idle);
            continue;
        }
        let cpu = id.parse::<usize>()?;
        if let Some(&group) = cpu_groups.get(cpu).filter(|&&group| group != 0) {
            groups[group].0 += total;
            groups[group].1 += idle;
        }
        if cpus.contains(&cpu) {
            groups[3].0 += total;
            groups[3].1 += idle;
        }
    }
    Ok(groups)
}

fn disk_read_bytes() -> Result<u64> {
    let io = fs::read_to_string("/proc/self/io")?;
    let value = io.lines().find_map(|line| line.strip_prefix("read_bytes:"))
        .ok_or("missing process read_bytes counter")?;
    Ok(value.trim().parse()?)
}

fn measure(
    cpus: &[usize],
    indexes: &[Index],
    query_sets: &[Vec<f32>],
    duration: Duration,
    warmup: Duration,
    _mode: &str,
    pin_workers: bool,
) -> Result<()> {
    let count = query_sets[0].len() / DIM;
    let stride = std::env::var("HNSW_LATENCY_STRIDE")
        .unwrap_or_else(|_| "67".to_owned())
        .parse::<usize>()?;
    if stride == 0 {
        return Err("latency sampling stride must be positive".into());
    }
    let mut order: Vec<_> = (0..count).collect();
    let mut random = 0x641d_5e4f_d21a_9bd1_u64;
    for end in (1..count).rev() {
        random ^= random << 13;
        random ^= random >> 7;
        random ^= random << 17;
        order.swap(end, random as usize % (end + 1));
    }
    let (block, passes) = if let Ok(block) = std::env::var("HNSW_QUERY_BLOCK") {
        let block = block.parse::<usize>()?;
        if block == 0 || count % block != 0 {
            return Err("query block size must divide the number of queries".into());
        }
        (block, 4)
    } else {
        (count, 1)
    };
    if passes > 1 {
        order = order
            .chunks(block)
            .flat_map(|chunk| (0..passes).flat_map(move |_| chunk.iter().copied()))
            .collect();
    }

    let ready = Barrier::new(cpus.len() + 1);
    let gate = Barrier::new(cpus.len() + 1);
    let start = OnceLock::new();
    let cpu_groups = cpu_groups()?;
    let monitored_cpus = if pin_workers { cpus.to_vec() } else { allowed_cpus()? };
    let mut perf = Perf::open()?;
    let (workers, elapsed, host, disk_read_bytes) = thread::scope(|scope| -> Result<_> {
        let threads: Vec<_> = cpus
            .iter()
            .enumerate()
            .map(|(id, &cpu)| {
                let index = &indexes[id % indexes.len()];
                let queries = &query_sets[id % query_sets.len()];
                let (ready, gate, start, order, cpu_groups) = (&ready, &gate, &start, &order, &cpu_groups);
                scope.spawn(move || {
                    if pin_workers {
                        if let Err(err) = pin(cpu) {
                            eprintln!("cannot pin worker {id} to CPU {cpu}: {err}");
                            std::process::abort();
                        }
                    }
                    let initial = id * 331 % count;
                    let mut pos = initial / block * block * passes + initial % block;
                    let mut search = || {
                        let offset = order[pos] * DIM;
                        pos += 1;
                        if pos == order.len() {
                            pos = 0;
                        }
                        let query = &queries[offset..offset + DIM];
                        #[cfg(feature = "diagnostic")]
                        if _mode == "explicit" {
                            // SAFETY: each worker owns a distinct reserved slot.
                            return unsafe { index.search_f32_on_slot(query, K, id) };
                        }
                        index.search(query, K)
                    };
                    let warm_until = Instant::now() + warmup;
                    while Instant::now() < warm_until {
                        if let Err(err) = black_box(search()) {
                            eprintln!("warmup search failed: {err}");
                            std::process::abort();
                        }
                    }

                    let mut row = Worker {
                        latency_ns: Vec::with_capacity(16_384),
                        ..Worker::default()
                    };
                    ready.wait();
                    gate.wait();
                    let before = usage();
                    let deadline = *start.get().unwrap() + duration;
                    let mut next_sample = id % stride;
                    while Instant::now() < deadline {
                        for _ in 0..16 {
                            let sampled = (next_sample == 0).then(Instant::now);
                            next_sample = if next_sample == 0 {
                                stride - 1
                            } else {
                                next_sample - 1
                            };
                            #[cfg(feature = "slot-timing")]
                            if sampled.is_some() {
                                usearch::begin_slot_wait_sample();
                            }
                            let result = search();
                            #[cfg(feature = "slot-timing")]
                            if let Some(sampled) = sampled {
                                let ns = sampled.elapsed().as_nanos() as u64;
                                let (take, put) = usearch::end_slot_wait_sample();
                                row.slot_wait.push((take, put, ns));
                            }
                            match result {
                                Ok(result) if result.keys.len() == K => {
                                    black_box(result);
                                    row.good += 1;
                                    if let Some(sampled) = sampled {
                                        row.latency_ns.push(sampled.elapsed().as_nanos() as u64);
                                        // SAFETY: sched_getcpu takes no pointers and reports the current CPU.
                                        let current = unsafe { libc::sched_getcpu() };
                                        if let Some(&node) = cpu_groups.get(current as usize).filter(|&&node| node > 0) {
                                            row.sampled_nodes[node - 1] += 1;
                                        }
                                    }
                                }
                                Ok(_) => row.short += 1,
                                Err(err) => {
                                    if row.failed == 0 {
                                        eprintln!("CPU {cpu} search failed: {err}");
                                    }
                                    row.failed += 1;
                                }
                            }
                        }
                    }
                    let after = usage();
                    row.cpu = cpu_seconds(&after) - cpu_seconds(&before);
                    row.minor = after.ru_minflt - before.ru_minflt;
                    row.major = after.ru_majflt - before.ru_majflt;
                    row.involuntary = after.ru_nivcsw - before.ru_nivcsw;
                    row.voluntary = after.ru_nvcsw - before.ru_nvcsw;
                    row
                })
            })
            .collect();
        ready.wait();
        let read_before = disk_read_bytes()?;
        let host_before = host_ticks(&cpu_groups, &monitored_cpus);
        let enabled = perf.iter_mut().try_for_each(|p| p.command("enable"));
        let begin = Instant::now();
        start.set(begin).unwrap();
        gate.wait();
        let rows: Vec<_> = threads
            .into_iter()
            .map(|worker| worker.join().unwrap())
            .collect();
        let elapsed = begin.elapsed();
        let disabled = perf.iter_mut().try_for_each(|p| p.command("disable"));
        enabled?;
        disabled?;
        let before = host_before?;
        let after = host_ticks(&cpu_groups, &monitored_cpus)?;
        let idle_fraction = |group: usize| {
            (after[group].1 - before[group].1) as f64 / (after[group].0 - before[group].0) as f64
        };
        let clock_ticks = unsafe { libc::sysconf(libc::_SC_CLK_TCK) } as f64;
        let busy_cores = |group: usize| {
            let total = after[group].0 - before[group].0;
            let idle = after[group].1 - before[group].1;
            (total - idle) as f64 / clock_ticks / elapsed.as_secs_f64()
        };
        Ok((
            rows,
            elapsed,
            [100.0 * idle_fraction(0), busy_cores(1), busy_cores(2), busy_cores(3)],
            disk_read_bytes()? - read_before,
        ))
    })?;
    let [host_idle_pct, host_busy_n0, host_busy_n1, host_busy_query_cpus] = host;

    let mut latency: Vec<_> = workers
        .iter()
        .flat_map(|row| row.latency_ns.iter().copied())
        .collect();
    latency.sort_unstable();
    let percentile = |p: usize| (latency[(latency.len() * p).div_ceil(100) - 1] as f64) / 1e3;
    let good: u64 = workers.iter().map(|w| w.good).sum();
    let failed: u64 = workers.iter().map(|w| w.failed).sum();
    let short: u64 = workers.iter().map(|w| w.short).sum();
    let cpu: f64 = workers.iter().map(|w| w.cpu).sum();
    let minor: i64 = workers.iter().map(|w| w.minor).sum();
    let major: i64 = workers.iter().map(|w| w.major).sum();
    let involuntary: i64 = workers.iter().map(|w| w.involuntary).sum();
    let voluntary: i64 = workers.iter().map(|w| w.voluntary).sum();
    let seconds = elapsed.as_secs_f64();
    if latency.is_empty() {
        return Err("no latency samples".into());
    }
    println!("seconds={seconds:.6},completed={good},errors={failed},short={short},qps={:.3},cpu_cores={:.4},host_idle_pct={host_idle_pct:.3},cpu_us_per_query={:.4},p50_us={:.3},p95_us={:.3},p99_us={:.3},samples={},minor_faults={minor},major_faults={major},involuntary_switches={involuntary},voluntary_switches={voluntary},latency_stride={stride},host_busy_n0={host_busy_n0:.3},host_busy_n1={host_busy_n1:.3},host_busy_query_cpus={host_busy_query_cpus:.3},disk_read_bytes={disk_read_bytes}",
        good as f64 / seconds, cpu / seconds, 1e6 * cpu / good as f64,
        percentile(50), percentile(95), percentile(99), latency.len());
    for (&cpu_id, worker) in cpus.iter().zip(&workers) {
        let cpu_id = if pin_workers { cpu_id as isize } else { -1 };
        eprintln!(
            "cpu={cpu_id},completed={},busy_pct={:.3},involuntary_switches={},sampled_node0={},sampled_node1={}",
            worker.good,
            100.0 * worker.cpu / seconds,
            worker.involuntary, worker.sampled_nodes[0], worker.sampled_nodes[1]
        );
    }
    #[cfg(feature = "slot-timing")]
    for (take, put, latency) in workers.iter().flat_map(|worker| &worker.slot_wait) {
        eprintln!("slot_wait_ns={take},{put},{latency}");
    }
    assert_eq!(failed + short, 0);
    Ok(())
}

pub fn run(args: &[String]) -> Result<()> {
    match args.get(1).map(String::as_str) {
        #[cfg(feature = "diagnostic")]
        Some("work") if args.len() == 5 => {
            let index = open_view(&args[2], args[4].parse()?, 1)?;
            let path = Path::new(&args[3]);
            let queries = vectors(path, query_count(path)?)?;
            graph_work(&index, &queries)
        }
        Some("recall") if args.len() == 6 => {
            let index = open_view(&args[2], args[5].parse()?, 1)?;
            let queries = vectors(Path::new(&args[3]), 1000)?;
            recall(&index, &queries, &args[4])
        }
        Some("bench") if matches!(args.len(), 9 | 10) => {
            let path = Path::new(&args[2]).canonicalize()?;
            let query_path = Path::new(&args[3]);
            let query_count = query_count(query_path)?;
            let mut query_sets = vec![vectors(query_path, query_count)?];
            let pin_workers = !args[6].starts_with("unbound:");
            let cpus = if let Some(threads) = args[6].strip_prefix("unbound:") {
                let threads = threads.parse::<usize>()?;
                if threads == 0 || threads > libc::CPU_SETSIZE as usize {
                    return Err("unbound worker count is out of range".into());
                }
                if allowed_cpus()?.len() < 72 {
                    return Err("unbound experiment requires access to all 72 host CPUs".into());
                }
                (0..threads).collect::<Vec<_>>()
            } else {
                args[6].split(',').map(str::parse::<usize>)
                    .collect::<std::result::Result<Vec<_>, _>>()?
            };
            if !pin_workers && args[4] != "shared" {
                return Err("unbound workers require shared mode".into());
            }
            assert!(cpus.iter().all(|&cpu| cpu < libc::CPU_SETSIZE as usize));
            let ef = args[5].parse()?;
            let (indexes, slots, (pages0, pages1)) = if args[4] == "pair" {
                let second = Path::new(args.get(9).ok_or("pair requires a second index path")?)
                    .canonicalize()?;
                if cpus.is_empty() || cpus.len() % 2 != 0 {
                    return Err("pair requires alternating CPUs from both sockets".into());
                }
                let slots = cpus.len() / 2;
                let mut indexes = Vec::with_capacity(2);
                for (id, file) in [&path, &second].into_iter().enumerate() {
                    pin(cpus[id])?;
                    if id == 1 {
                        query_sets.push(vectors(query_path, query_count)?);
                    }
                    indexes.push(open_view(file.to_str().unwrap(), ef, slots)?);
                }
                pin(cpus[0])?;
                let first_pages = prefault(&path, if path == second { 2 } else { 1 })?;
                let (a, b) = if path == second {
                    // Each VMA points at the same physical file pages.
                    if first_pages.0 % 2 != 0 || first_pages.1 % 2 != 0 {
                        return Err("paired mappings have inconsistent page counts".into());
                    }
                    let per_view = (first_pages.0 / 2, first_pages.1 / 2);
                    (per_view, per_view)
                } else {
                    (first_pages, prefault(&second, 1)?)
                };
                eprintln!("view_pages={},{};{},{}", a.0, a.1, b.0, b.1);
                (indexes, slots, (a.0 + b.0, a.1 + b.1))
            } else {
                if args.len() != 9 || cpus.is_empty() {
                    return Err("bench requires CPUs and takes a second path only in pair mode".into());
                }
                let (maps, slots) = match args[4].as_str() {
                    "shared" => (1, cpus.len()),
                    #[cfg(feature = "diagnostic")]
                    "explicit" => (1, cpus.len()),
                    "private" => (cpus.len(), 1),
                    _ => return Err("mode must be shared, private or pair".into()),
                };
                let indexes = (0..maps)
                    .map(|_| open_view(path.to_str().unwrap(), ef, slots))
                    .collect::<Result<Vec<_>>>()?;
                (indexes, slots, prefault(&path, maps)?)
            };
            println!("rows={},maps={},slots_per_index={slots},mapped_pages_n0={pages0},mapped_pages_n1={pages1},query_count={query_count},query_copies={},kernel={}",
                indexes[0].size(), indexes.len(), query_sets.len(), indexes[0].hardware_acceleration());
            measure(&cpus, &indexes, &query_sets, Duration::from_secs_f64(args[7].parse()?),
                Duration::from_secs_f64(args[8].parse()?), &args[4], pin_workers)?;
            if args[4] == "pair" {
                let second = Path::new(&args[9]).canonicalize()?;
                let mut a = mapped_pages(&path)?;
                let b = if path == second {
                    a = (a.0 / 2, a.1 / 2);
                    a
                } else {
                    mapped_pages(&second)?
                };
                eprintln!("post_view_pages={},{};{},{}", a.0, a.1, b.0, b.1);
            } else {
                let (n0, n1) = mapped_pages(&path)?;
                eprintln!("post_view_pages={n0},{n1}");
            }
            Ok(())
        }
        _ => Err("usage: build TRAIN INDEX ROWS WORKERS | recall INDEX QUERIES TRUTH EF | bench INDEX QUERIES MODE EF CPU-LIST SECONDS WARMUP [SECOND-INDEX]".into()),
    }
}
