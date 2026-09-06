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
 */

interface Params {
  url: string; // asset 的 API 地址
  output: string; // 落盘路径
  token?: string; // GitHub 访问令牌
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

// ---------- 下载 ----------
try {
  const headers: Record<string, string> = {
    Accept: "application/octet-stream",
  };
  if (params.token) headers.Authorization = `Bearer ${params.token}`;

  const res = await fetch(params.url, { headers });
  if (!res.ok || !res.body) {
    emit({
      type: "error",
      message: `下载失败 (HTTP ${res.status}${res.statusText ? " " + res.statusText : ""})`,
    });
    Deno.exit(1);
  }

  const total = Number(res.headers.get("content-length") || 0);
  const file = await Deno.open(params.output, { write: true, create: true, truncate: true });
  try {
    const reader = res.body.getReader();
    let received = 0;
    let lastReport = 0;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      await file.write(value);
      received += value.length;

      // 进度节流：每 512KB 或首次上报一次
      if (received - lastReport >= 512 * 1024 || received === value.length) {
        emit({ type: "progress", received, total });
        lastReport = received;
      }
    }
    emit({ type: "progress", received, total });
    emit({ type: "done", size: received, path: params.output });
  } finally {
    file.close();
  }
} catch (e) {
  // 清理残留的不完整文件
  try {
    await Deno.remove(params.output);
  } catch {
    // 忽略清理失败
  }
  emit({ type: "error", message: `下载中断: ${e}` });
  Deno.exit(1);
}
