# pyramid-tiler

纯本地的图像金字塔（image pyramid）与切片（tile）生成工具。所有输入图片、
缩放层、tile、manifest 和缓存只存在于本地文件或内存中——不依赖地图服务器、
CDN、云存储或任何外部服务，运行时无任何网络访问。

## 安装与依赖

- Python 3.10+
- 依赖：`Pillow`、`numpy`（测试需要 `pytest`）

```bash
pip install pillow numpy pytest
```

## 使用

```bash
# 构建（或增量重建）金字塔
python -m pyramid_tiler build input.jpg -o out/ \
    --tile-size 256 --resample bilinear --min-size 256

# 校验输出目录中所有 tile 与 manifest 的哈希是否一致
python -m pyramid_tiler verify out/
```

支持读取常见 PNG / JPEG（含灰度、调色板、带 alpha 的 PNG，统一规范化为
RGB 或 RGBA）。所有输出 tile 均为无损 PNG，保证字节级稳定。

## 层级算法

- 第 0 级为原图（全分辨率）。
- 每一级由上一级**精确缩小 2 倍**得到，宽高采用向上取整除法
  `(n + 1) // 2`，因此奇数尺寸不会丢失任何边缘像素。
- 当某一级满足 `max(width, height) <= min_size` 时停止，该级为最后一级。
- 缩放方式 `--resample`：
  - `nearest`：像素中心映射后取最近源像素，输出必是源图像素的子集；
  - `bilinear`：像素中心映射（align_corners=False）的双线性插值，
    先沿 x 后沿 y，float64 累加、单次取整。
- 两种方法均直接在 numpy 上实现，不依赖第三方重采样内部行为，
  **相同输入在任何机器上得到逐字节相同的结果**。

## 坐标与切片规则

- 每级按固定 `tile_size`（默认 256）切成网格，原点在**左上角**。
- tile `(z, x, y)` 覆盖第 `z` 级的像素区域
  `[x*ts, (x+1)*ts) × [y*ts, (y+1)*ts)`，文件存为 `tiles/{z}/{x}_{y}.png`。
- **边缘规则（padding）**：右/下边缘不足一个完整 tile 时，用零值像素
  补齐到完整 `tile_size × tile_size`（RGB 为黑色，RGBA 为透明）。
  因此磁盘上每个 tile 尺寸完全一致；tile 内有效像素范围由 manifest 中的
  `content_width` / `content_height` 记录，消费者据此裁剪即可。

## Manifest

`out/manifest.json` 是增量重建的唯一事实来源，内容包括：

- `source`：原图文件名、宽高、SHA-256；
- `config`：`tile_size` / `resample` / `min_size`；
- `levels[]`：每级的 `z`、宽高、网格行列数，以及每个 tile 的
  `x`、`y`、文件路径、存储尺寸、有效内容尺寸和文件 SHA-256。

manifest 使用固定键序序列化，相同输入得到逐字节相同的 manifest。

## 增量重建与原子写

- 重建时若 manifest 中的源图哈希与配置均匹配，则逐 tile 校验磁盘文件的
  SHA-256：**存在且哈希一致的 tile 直接跳过**；损坏、被截断或缺失的
  tile 只重新生成受影响的部分；源图或配置变化则整体重建。
- 所有文件（tile 与 manifest）都通过「同目录临时文件 + fsync +
  `os.replace`」原子写入，manifest 永远最后发布。生成中断只会留下
  不会被 manifest 引用的 `*.tmp` 临时文件，绝不会出现被 manifest
  误认为有效的半文件；重新运行即可从混乱状态恢复到完整一致。

## 测试

全部测试在终端运行并输出校验结果，不打开任何图片窗口；测试图像由代码
现场生成（确定性的渐变+棋盘合成图，无随机性）：

```bash
python -m pytest tests/ -v
```

覆盖场景：奇数尺寸层级推算、边缘 tile 的 padding 与内容尺寸、
nearest/bilinear 两种缩放及其确定性、PNG/JPEG/alpha 输入、manifest
内容与哈希校验、增量跳过、tile 损坏/缺失/截断后的定点重生成、
源图变化触发全量重建、中途崩溃不留半文件、残留临时文件不被误用。
