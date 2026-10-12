/**
 * GitHub Release 资产下载脚本（Deno 运行时）
 *
 * 由 Python 端通过 QProcess 调用：
 *   deno run --allow-net --allow-write download.ts <json参数>
 *
 * 协议约定（stdout 每行一个 JSON）：
 *   {"type":"progress","received":123,"total":456}   进度上报（节流）
 *   {"type":"done","size":456,"path":"..."}          下载完成
 *   {"type":"error","message":"..."}                 失败（随后 exit 1）
 *
 * 断点续传：参数传 start>0 时以 `Range: bytes=start-` 请求并追加写入，
 * 服务器不支持续传（返回 200 完整内容）时自动回退为从头下载（truncate）。
 * 失败/被终止时保留半成品文件，便于外层恢复（不再自动删除）。
 */

interface Params {
  url: string; // asset 的 API 地址
  output: string; // 落盘路径
  token?: string; // GitHub 访问令牌
  start?: number; // 断点续传起始字节偏移
  total?: number; // 完整大小（续传时用作进度分母；首次由 content-length 推断）
}

const decoder = new TextDecoder();

function emit(obj: Record<string, unknown>): void {
  console.log(JSON.stringify(obj));
}

// ---------- 解析参数 ----------
let params: Params;
try {
  params = JSON.parse(decoder.decode(Deno.readFileSync(Deno.args[0])));
} catch (e) {
  emit({ type: "error", message: `参数解析失败: ${e}` });
  Deno.exit(1);
}

if (!params.url || !params.output) {
  emit({ type: "error", message: "缺少 url 或 output 参数" });
  Deno.exit(1);
}

// ---------- 校正续传偏移（以磁盘上已有文件大小为准） ----------
let start = Math.max(0, Math.floor(Number(params.start || 0)));
if (start > 0) {
  try {
    const st = await Deno.stat(params.output);
    if (st.isFile && st.size !== start) {
      start = st.size; // 以实际大小为准
    }
  } catch {
    start = 0; // 文件不存在 → 从头下载
  }
}

// ---------- 下载 ----------
try {
  const headers: Record<string, string> = {
    Accept: "application/octet-stream",
  };
  if (params.token) headers.Authorization = `Bearer ${params.token}`;
  if (start > 0) headers.Range = `bytes=${start}-`;

  const res = await fetch(params.url, { headers });
  // 200（完整/不支持续传）或 206（续传）都视为可继续
  if (!res.ok && res.status !== 206) {
    emit({
      type: "error",
      message: `下载失败 (HTTP ${res.status}${res.statusText ? " " + res.statusText : ""})`,
    });
    Deno.exit(1);
  }
  if (!res.body) {
    emit({ type: "error", message: "响应无正文" });
    Deno.exit(1);
  }

  // 服务器忽略 Range 返回 200 完整内容 → 回退为从头下载
  let effectiveStart = start;
  if (start > 0 && res.status !== 206) {
    effectiveStart = 0;
  }

  // total：优先用外层传入的完整大小；否则由 content-length 推断
  const contentLength = Number(res.headers.get("content-length") || 0);
  let total: number;
  if (params.total && params.total > 0) {
    total = params.total;
  } else if (effectiveStart > 0) {
    total = effectiveStart + contentLength; // 206 时 content-length 为剩余字节数
  } else {
    total = contentLength;
  }

  const openFlags = effectiveStart > 0
    ? { write: true, create: true, append: true }
    : { write: true, create: true, truncate: true };
  const file = await Deno.open(params.output, openFlags);
  try {
    const reader = res.body.getReader();
    let received = effectiveStart;
    let lastReport = effectiveStart;
    let firstChunk = true;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      await file.write(value);
      received += value.length;

      // 进度节流：每 512KB 或首个数据块上报一次
      if (firstChunk || received - lastReport >= 512 * 1024) {
        emit({ type: "progress", received, total });
        lastReport = received;
        firstChunk = false;
      }
    }
    emit({ type: "progress", received, total });
    emit({ type: "done", size: received, path: params.output });
  } finally {
    file.close();
  }
} catch (e) {
  // 保留半成品文件以便外层恢复（不再自动删除）
  emit({ type: "error", message: `下载中断: ${e}` });
  Deno.exit(1);
}
