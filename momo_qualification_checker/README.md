# MoMo 资格批量查询（脱敏独立工具）

## 启动

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe momo_qualification_checker\app.py --host 127.0.0.1 --port 5013
```

然后打开 <http://127.0.0.1:5013/>。

## 使用

1. 在 **AT** 文本框中每行填写一个 Access Token；也支持 `备注----AT`。
2. 在 **VN 代理池** 中每行填写一条代理，例如 `VN|http://host:port` 或 `socks5h://user:pass@host:port`。
3. 设置并发和每个 AT 的最大代理尝试次数，点击“开始查询”。
4. 临时网络错误会换下一条代理重试；明确无 MoMo 支付方式时立即结束该 AT。

AT、代理和结果只在内存中处理；程序不写入本站账号 JSON、Token 文件或日志文件。界面只显示脱敏 AT 预览和脱敏代理信息。

## 检测边界

工具复用本站已验证的 `detect_momo`：创建 VN/VND Checkout 并读取 MoMo 支付方式证据；不会执行税费、PaymentMethod、confirm、approve、轮询或支付扣款。

本目录不包含任何真实 AT、代理、Cookie、密钥或 Checkout URL。实际探针实现通过 `core.integrated_runtime` 加载仓库内已锁定的 PAY.153 代码。
