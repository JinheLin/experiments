mod query;

use std::error::Error;
use std::fs::File;
use std::io::{self, Read};
use std::mem::size_of;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;
use std::time::Instant;
use usearch::{Index, IndexOptions, MetricKind, ScalarKind};

type Result<T> = std::result::Result<T, Box<dyn Error>>;
const DIM: usize = 768;
const K: usize = 10;

fn new_index(ef: usize) -> Result<Index> {
    Ok(Index::new(&IndexOptions {
        dimensions: DIM,
        metric: MetricKind::L2sq,
        quantization: ScalarKind::F32,
        connectivity: 16,
        expansion_add: 128,
        expansion_search: ef,
        multi: false,
    })?)
}

fn vectors(path: &Path, rows: usize) -> io::Result<Vec<f32>> {
    assert!(cfg!(target_endian = "little"));
    let mut data = vec![0.0f32; rows * DIM];
    // SAFETY: every bit pattern is a valid f32 and the slice spans initialized storage.
    let bytes = unsafe {
        std::slice::from_raw_parts_mut(
            data.as_mut_ptr().cast::<u8>(),
            data.len() * size_of::<f32>(),
        )
    };
    File::open(path)?.read_exact(bytes)?;
    Ok(data)
}

fn build(train: &Path, output: &str, rows: usize, workers: usize) -> Result<()> {
    let data = vectors(train, rows)?;
    let index = new_index(64)?;
    index.reserve_capacity_and_threads(rows, workers)?;
    let next = AtomicUsize::new(0);
    let start = Instant::now();
    thread::scope(|scope| {
        for _ in 0..workers {
            scope.spawn(|| loop {
                let first = next.fetch_add(32, Ordering::Relaxed);
                if first >= rows {
                    break;
                }
                for i in first..(first + 32).min(rows) {
                    index
                        .add(i as u64, &data[i * DIM..(i + 1) * DIM])
                        .expect("add");
                }
                if first % 65_536 == 0 {
                    eprintln!(
                        "insert block {first}/{rows}, {:.1}s",
                        start.elapsed().as_secs_f64()
                    );
                }
            });
        }
    });
    assert_eq!(index.size(), rows);
    let partial = format!("{output}.partial");
    index.save(&partial)?;
    std::fs::rename(partial, output)?;
    println!(
        "rows={rows},workers={workers},kernel={},seconds={:.3},bytes={}",
        index.hardware_acceleration(),
        start.elapsed().as_secs_f64(),
        std::fs::metadata(output)?.len()
    );
    Ok(())
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("build") if args.len() == 6 => build(
            Path::new(&args[2]),
            &args[3],
            args[4].parse()?,
            args[5].parse()?,
        ),
        _ => query::run(&args),
    }
}
