#include <inttypes.h>
#include <linux/mempolicy.h>
#include <omp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

static void bind_range(void *start, size_t length, int policy, unsigned long nodes) {
    if (syscall(SYS_mbind, start, length, policy, &nodes, 8 * sizeof(nodes), 0) != 0) {
        perror("mbind");
        exit(1);
    }
}

static void verify_pages(void *start, size_t length, int layout) {
    FILE *maps = fopen("/proc/self/numa_maps", "r");
    if (!maps) {
        perror("numa_maps");
        exit(1);
    }
    char line[2048];
    unsigned long n0 = 0, n1 = 0;
    while (fgets(line, sizeof(line), maps)) {
        uintptr_t addr = strtoull(line, NULL, 16);
        if (addr < (uintptr_t)start || addr >= (uintptr_t)start + length)
            continue;
        const char *p0 = strstr(line, " N0=");
        const char *p1 = strstr(line, " N1=");
        n0 += p0 ? strtoul(p0 + 4, NULL, 10) : 0;
        n1 += p1 ? strtoul(p1 + 4, NULL, 10) : 0;
    }
    fclose(maps);
    unsigned long expected = length / (size_t)sysconf(_SC_PAGESIZE);
    unsigned long tolerance = expected / 1000;
    unsigned long target_n0 = layout < 0 ? expected / 2 : layout == 0 ? expected : 0;
    unsigned long target_n1 = expected - target_n0;
    if (n0 + tolerance < target_n0 || n0 > target_n0 + tolerance ||
        n1 + tolerance < target_n1 || n1 > target_n1 + tolerance) {
        fprintf(stderr, "Unexpected scan page placement. N0=%lu N1=%lu\n", n0, n1);
        exit(1);
    }
    printf("pages_n0=%lu,pages_n1=%lu\n", n0, n1);
}

int main(int argc, char **argv) {
    if (argc < 2 || argc > 3) {
        fprintf(stderr, "usage: stream-read node0|node1|dual|dual-local [SECONDS]\n");
        return 1;
    }
    double duration = argc == 3 ? atof(argv[2]) : 4.0;
    if (!(duration > 0))
        return 1;
    int layout = strcmp(argv[1], "node0") == 0 ? 0 : strcmp(argv[1], "node1") == 0 ? 1 : -1;
    size_t length = (size_t)1536 * 1024 * 1024;
    size_t elements = length / sizeof(uint64_t);
    uint64_t *data = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (data == MAP_FAILED || madvise(data, length, MADV_NOHUGEPAGE) != 0) {
        perror("map scan buffer");
        return 1;
    }
    if (strcmp(argv[1], "dual") == 0) {
        const size_t stripe = 2 * 1024 * 1024;
        for (size_t offset = 0; offset < length; offset += stripe)
            bind_range((char *)data + offset, stripe, MPOL_BIND, 1UL << ((offset / stripe) % 2));
    } else if (strcmp(argv[1], "dual-local") == 0) {
        bind_range(data, length / 2, MPOL_BIND, 1);
        bind_range(data + elements / 2, length / 2, MPOL_BIND, 2);
    } else if (layout >= 0) {
        bind_range(data, length, MPOL_BIND, 1UL << layout);
    } else {
        return 1;
    }
    uint64_t checksum = 0, bytes = 0;
    double elapsed;
    #pragma omp parallel reduction(+:checksum, bytes)
    {
        size_t workers = omp_get_num_threads();
        size_t id = omp_get_thread_num();
        size_t first = elements * id / workers;
        size_t end = elements * (id + 1) / workers;
        for (size_t i = first; i < end; ++i)
            data[i] = i;
        #pragma omp barrier
        if (argc == 3) {
            #pragma omp single
            {
                verify_pages(data, length, layout);
                fflush(stdout);
            }
        }
        double start = omp_get_wtime();
        do {
            // Make each pass observable to the optimizer while leaving memory read-only.
            asm volatile("" : : "r"(data) : "memory");
            uint64_t local = 0;
            #pragma omp simd reduction(+:local)
            for (size_t i = first; i < end; ++i)
                local += data[i];
            checksum += local;
            bytes += (end - first) * sizeof(*data);
        } while (omp_get_wtime() - start < duration);
        #pragma omp barrier
        #pragma omp master
        elapsed = omp_get_wtime() - start;
    }
    printf("seconds=%.6f,read_GiB_per_sec=%.3f,checksum=%" PRIu64 "\n",
           elapsed, bytes / elapsed / (1024 * 1024 * 1024), checksum);
    verify_pages(data, length, layout);
    munmap(data, length);
}
