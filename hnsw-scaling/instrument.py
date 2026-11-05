#!/usr/bin/env python3
"""Add a benchmark-only Rust FFI for fixed slot IDs and existing graph counters."""
import argparse
from pathlib import Path
import shutil


def patch(path, changes):
    source = path.read_text()
    for before, after in changes:
        assert source.count(before) == 1, (path, before)
        source = source.replace(before, after)
    path.write_text(source)



def instrument_slot_wait(crate, rust):
    patch(crate / "include/usearch/index_dense.hpp", [
        ('#include "index.hpp"', '#include "index.hpp"\n#include <chrono>'),
        (
            "template <typename, typename> class index_dense_gt;",
            """struct slot_wait_sample_t {
    std::uint64_t take_ns = 0, put_ns = 0;
};
inline thread_local slot_wait_sample_t* active_slot_wait_sample = nullptr;
using slot_wait_clock_t = std::chrono::steady_clock;

template <typename, typename> class index_dense_gt;""",
        ),
        (
            "        std::unique_lock<std::mutex> lock(available_threads_mutex_);\n        if (!available_threads_.try_pop(thread_id))",
            """        auto* sample = active_slot_wait_sample;
        auto start = sample ? slot_wait_clock_t::now() : slot_wait_clock_t::time_point{};
        std::unique_lock<std::mutex> lock(available_threads_mutex_);
        if (sample)
            sample->take_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(slot_wait_clock_t::now() - start).count();
        if (!available_threads_.try_pop(thread_id))""",
        ),
        (
            "        available_threads_mutex_.lock();\n        usearch_assert_m",
            """        auto* sample = active_slot_wait_sample;
        auto start = sample ? slot_wait_clock_t::now() : slot_wait_clock_t::time_point{};
        available_threads_mutex_.lock();
        if (sample)
            sample->put_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(slot_wait_clock_t::now() - start).count();
        usearch_assert_m""",
        ),
    ])
    patch(rust / "lib.rs", [
        (
            '    unsafe extern "C++" {\n        include!("lib.hpp");',
            """    struct SlotWait {
        take_ns: u64,
        put_ns: u64,
    }

    unsafe extern "C++" {
        include!("lib.hpp");
        pub fn begin_slot_wait_sample();
        pub fn end_slot_wait_sample() -> SlotWait;""",
        ),
        (
            "pub fn hardware_acceleration_compiled() -> String {",
            """pub fn begin_slot_wait_sample() { ffi::begin_slot_wait_sample(); }
pub fn end_slot_wait_sample() -> (u64, u64) {
    let sample = ffi::end_slot_wait_sample();
    (sample.take_ns, sample.put_ns)
}

pub fn hardware_acceleration_compiled() -> String {""",
        ),
    ])
    patch(rust / "lib.hpp", [
        ("struct Matches;", "struct Matches;\nstruct SlotWait;\nvoid begin_slot_wait_sample();\nSlotWait end_slot_wait_sample();"),
    ])
    patch(rust / "lib.cpp", [
        (
            "using index_t = index_dense_t;",
            """static thread_local slot_wait_sample_t slot_wait_storage;

void begin_slot_wait_sample() {
    slot_wait_storage = {};
    active_slot_wait_sample = &slot_wait_storage;
}
SlotWait end_slot_wait_sample() {
    active_slot_wait_sample = nullptr;
    return {slot_wait_storage.take_ns, slot_wait_storage.put_ns};
}

using index_t = index_dense_t;""",
        ),
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("crate", type=Path, help="unmodified usearch-2.25.3 crate")
    parser.add_argument("output", type=Path)
    parser.add_argument("--slot-timing", action="store_true")
    args = parser.parse_args()
    dest = args.output / "usearch"
    shutil.copytree(args.crate, dest)
    rust = dest / "rust"
    patch(rust / "lib.rs", [
        ("        distances: Vec<f32>,\n    }", "        distances: Vec<f32>,\n        visited: usize,\n        computed: usize,\n    }"),
        (
            "        pub fn search_f32(self: &NativeIndex, query: &[f32], count: usize) -> Result<Matches>;",
            "        pub fn search_f32(self: &NativeIndex, query: &[f32], count: usize) -> Result<Matches>;\n"
            "        pub fn search_f32_on_slot(self: &NativeIndex, query: &[f32], count: usize, slot: usize) -> Result<Matches>;",
        ),
        (
            "impl Index {",
            "impl Index {\n"
            "    /// # Safety\n"
            "    /// The caller must exclusively own this reserved slot and may not mix with pooled calls.\n"
            "    pub unsafe fn search_f32_on_slot(&self, query: &[f32], count: usize, slot: usize)\n"
            "        -> Result<ffi::Matches, cxx::Exception> {\n"
            "        self.inner.search_f32_on_slot(query, count, slot)\n"
            "    }\n",
        ),
    ])
    patch(rust / "lib.hpp", [
        (
            "    Matches search_f32(rust::Slice<float const> query, size_t count) const;",
            "    Matches search_f32(rust::Slice<float const> query, size_t count) const;\n"
            "    Matches search_f32_on_slot(rust::Slice<float const> query, size_t count, size_t slot) const;",
        ),
    ])
    patch(rust / "lib.cpp", [
        (
            "size_t vec_dims, size_t count, bool exact = false) {",
            "size_t vec_dims, size_t count, bool exact = false, size_t slot = index_dense_t::any_thread()) {",
        ),
        (
            "    search_result_t result = index.search(vec, count, index_dense_t::any_thread(), exact);\n    result.error.raise();",
            "    search_result_t result = index.search(vec, count, slot, exact);\n    result.error.raise();\n"
            "    matches.visited = result.visited_members;\n    matches.computed = result.computed_distances;",
        ),
        (
            "Matches NativeIndex::search_f32(rust::Slice<float const> vec, size_t count) const { return search_(*index_, vec.data(), vec.size(), count, false); }",
            "Matches NativeIndex::search_f32(rust::Slice<float const> vec, size_t count) const { return search_(*index_, vec.data(), vec.size(), count, false); }\n"
            "Matches NativeIndex::search_f32_on_slot(rust::Slice<float const> vec, size_t count, size_t slot) const { return search_(*index_, vec.data(), vec.size(), count, false, slot); }",
        ),
    ])
    if args.slot_timing:
        instrument_slot_wait(dest, rust)
    root = Path(__file__).resolve().parent
    manifest = (root / "Cargo.toml").read_text().replace(
        'name = "hnsw-scaling"', 'name = "hnsw-scaling-diagnostic"'
    )
    (args.output / "Cargo.toml").write_text(
        manifest + '\n[patch.crates-io]\nusearch = { path = "usearch" }\n'
    )
    lock = (root / "Cargo.lock").read_text().replace(
        'name = "hnsw-scaling"', 'name = "hnsw-scaling-diagnostic"'
    )
    packages = lock.split("[[package]]")
    for i, package in enumerate(packages):
        if package.startswith('\nname = "usearch"\n'):
            packages[i] = "".join(
                line for line in package.splitlines(keepends=True)
                if not line.startswith(("source = ", "checksum = "))
            )
    (args.output / "Cargo.lock").write_text("[[package]]".join(packages))
    shutil.copytree(root / "src", args.output / "src")


if __name__ == "__main__":
    main()
