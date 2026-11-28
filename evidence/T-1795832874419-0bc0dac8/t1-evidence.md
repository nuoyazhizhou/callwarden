# T1 执行证据（T-1795832874419-0bc0dac8 / REQ-RG-01）

- 卡片：T-1795832874419-0bc0dac8（根任务，批次 1）
- 产出：`rust_ext/src/daemon/resources.rs` 新模块（配置解析 + 环境侦测 + 档位映射）
- executor：callwarden-alm-executor-zhengdx / sess-executor-zhengdx-t1-20261128
- lease：L-1eb81cb97ed72fa8（fencing_counter=1）

## Steps 完成度（4/4）

| step | step_id | 符号 | commit | 状态 |
|---|---|---|---|---|
| #0 | S-…-0c255958 前 | ResourceConfig + DiskType + Default | 26cfda3 | done |
| #1 | S-…-0c255958 | load_resource_config + from_toml_str | 98a9255 | done |
| #2 | S-1795832874426-0c255958 | detect_host_profile + HostProfile | f3c0f42 | done |
| #3 | S-1795832874426-0c25a264 | apply_profile | 6171641 | done |

注：step#0/#1 的 step_id 以 daemon task.show 为准（#0 与 #1 同族前缀，commit message 内以 T1-S1/T1-S2 标识）。

## 实现要点

1. **C1.1 ResourceConfig**：七字段 + explicit_keys；Default 对齐现状（cache 2GB / workspaces 32=DEFAULT_SNAPSHOT_CACHE_CAPACITY / idle 1800s / workers 4=[build] 默认 / priority_low=true / disk_type=Auto / soft=0 禁用）。
2. **C1.2 load_resource_config**：`~/.callwarden/daemon.toml`（USERPROFILE/HOME 照 BuildPipelineConfig::config_path）；顶层键与 `[resources]` 表两种写法，顶层优先；缺文件静默默认（首启常态，T2 写回会补段）；读失败 WARN+默认；单键非法/类型错仅该键回退+WARN 不阻断；cache_max_bytes 拒 0，接受 512M/1G/纯字节数（口径对齐 Python `_is_valid_size_string`：K/M/G/T 大小写不敏感，拒小数/负数）；explicit_keys 出现即记录。
3. **C1.3 detect_host_profile**：HostProfile{total_ram_bytes, cpu_cores, disk_type}。RAM：Windows GlobalMemoryStatusEx / Unix /proc/meminfo MemTotal×1024 / 其他 0；核数：available_parallelism 失败回落 1；磁盘：Windows IOCTL_STORAGE_QUERY_PROPERTY BusType（NVMe=17→Ssd，其余 Unknown）/ Unix /sys/block rotational（排除 loop/ram/dm-）。全部 fail-soft WARN 不 panic。
4. **C1.4 apply_profile**：纯函数，返回被覆盖键列表（固定顺序 cache_max_bytes→cache_max_workspaces→build_parse_threads→memory_soft_limit_bytes）。RAM<24GB 档1（512MB/8/2/soft=max(2GB, RAM×20%)）；RAM≥24GB 档2 翻倍（1GB/16/4，soft 公式同档1）；生效 disk_type=Hdd → cache 封顶 512MB（档2 亦封顶；显式 disk_type≠Auto 优先于侦测值）；RAM=0 走档1；explicit_keys 键不覆盖不封顶不进 covered；idle_evict_secs/build_priority_low 不参与映射（卡面未列）。

## 测试实证

命令：`CARGO_BUILD_JOBS=2 cargo test --manifest-path rust_ext/Cargo.toml --lib daemon::resources`

结果：**24 passed; 0 failed; 0 ignored**（12 配置解析 + 6 环境侦测 + 6 档位映射），commit 6171641 工作树干净状态复现。

- AC-RG-01-2（显式覆盖优先于映射）：`ac_rg_01_2_explicit_overrides_mapping`——from_toml_str 显式 cache_max_bytes="1G"、build_parse_threads=6，apply_profile 档1 后仍保持 1G/6，covered 只含非显式键。
- AC-RG-01-3（损坏容错回退）：`ac_rg_01_3_corrupt_toml_falls_back_to_default` + 单键回退/类型错隔离/尺寸串边界等。
- AC-RG-01-4（容错分支）：parse_proc_meminfo_fail_soft、bus_type/rotational 映射、host_profile_fail_soft_values_constructible、detect_host_profile_smoke_current_platform（当前平台不 panic）。

## 与 contracts.md / 派工口径的偏差（如实登记）

1. **Cargo.toml feature 启用（+2）**：windows-sys 增启 `Win32_System_SystemInformation`（GlobalMemoryStatusEx）与 `Win32_System_Ioctl`（IOCTL_STORAGE_QUERY_PROPERTY）。无新依赖，仅启用既有依赖 feature；步骤 target_file 白名单未含 Cargo.toml，未列入 changes，已随 f3c0f42 入库。
2. **apply_profile 不复制 disk_type 到 cfg**：按 C1.4 卡面字面，映射只触四键；HDD 封顶判定用「生效 disk_type」（cfg 显式值优先，否则 profile 侦测值）。
3. **8GB 档口径差异**：派工口径提过「8GB→减半」，但任务卡与 contracts.md C1.4 只有 RAM<24GB / ≥24GB 两档。**按卡面两档实现**，8GB 主机走档1（512MB/8/2），无第三档。
4. **缺文件静默默认**：C1.2 卡面要求「文件缺失/损坏…回退默认」，实现细分：缺文件=静默（首启常态，避免每次首启 WARN 刷屏；T2 write-once 会补 [resources] 段），损坏/不可读=WARN。语义均为 fail-soft 不阻断。
5. **idle_evict_secs / build_priority_low 不参与映射**：C1.4 映射规则未列这两键，保持配置值/默认值。

## 边界与未覆盖

- detect_total_ram / detect_disk_type 的平台分支仅当前平台（Windows）smoke 实测；Unix 分支由纯函数单测覆盖（parse_proc_meminfo_memtotal、rotational_flags_to_disk_type），未在真实 Linux 跑。
- 内存软限的**执行**（RSS 采样、超限动作）属 T5+ 范围，本单只做配置面。
- write_resources_section_once（C1.5）属 T2，本单未实现。
