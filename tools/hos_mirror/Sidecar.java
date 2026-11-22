import com.huawei.hosscrcpy.api.HosRemoteConfig;
import com.huawei.hosscrcpy.api.HosRemoteDevice;
import com.huawei.hosscrcpy.api.ScreenCapCallback;
import java.net.*;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;

/** 自建鸿蒙投屏 sidecar：调用华为 hosScrcpy API 拿 H.264 帧，转发到本地 TCP 端口给 Python 解码。
 *  仅开发调试用，hosScrcpy.jar 版权归华为，不随产品分发。
 *  用法: java -cp hosScrcpy.jar Sidecar.java <sn> <hdcPath> <tcpPort> [frameRate]
 */
public class Sidecar {
    static final Object LOCK = new Object();

    public static void main(String[] args) throws Exception {
        if (args.length < 3) {
            System.err.println("usage: Sidecar <sn> <hdcPath> <tcpPort> [frameRate] [scale]");
            System.exit(2);
        }
        String sn = args[0], hdc = args[1];
        int port = Integer.parseInt(args[2]);
        int fps = args.length > 3 ? Integer.parseInt(args[3]) : 30;
        int scale = args.length > 4 ? Integer.parseInt(args[4]) : 1;

        ServerSocket srv = new ServerSocket(port, 1, InetAddress.getByName("127.0.0.1"));
        System.out.println("{\"ready\":true,\"port\":" + port + "}");
        System.out.flush();

        HosRemoteConfig cfg = new HosRemoteConfig(sn);
        cfg.setHdcPath(hdc);
        cfg.setFrameRate(fps);
        cfg.setScale(scale);   // int: 1 = 原始分辨率
        cfg.setBitRate(4 << 20);       // 4 Mbps
        cfg.setIFrameInterval(2000);

        final Socket[] client = {null};
        HosRemoteDevice dev = new HosRemoteDevice(cfg);
        ScreenCapCallback cb = new ScreenCapCallback() {
            public void onReady() {
                System.out.println("{\"event\":\"ready\"}"); System.out.flush();
            }
            public void onException(Throwable t) {
                System.err.println("{\"event\":\"exception\",\"msg\":\"" + t + "\"}");
                System.err.flush();
            }
            public void onData(ByteBuffer buf) {
                if (client[0] == null) {
                    // 懒接受：Python 连上来后开始推流
                    try { client[0] = srv.accept(); }
                    catch (Exception e) { return; }
                }
                int n = buf.remaining();
                byte[] data = new byte[n + 4];
                data[0]=(byte)(n>>>24); data[1]=(byte)(n>>>16); data[2]=(byte)(n>>>8); data[3]=(byte)n;
                buf.get(data, 4, n);
                try { client[0].getOutputStream().write(data); }
                catch (Exception e) { client[0] = null; }
            }
        };
        dev.startCaptureScreen(cb);

        // 保活：等待，Ctrl+C / stdin 关闭退出
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            try { dev.stopCaptureScreen(); } catch (Exception ignored) {}
            try { if (client[0]!=null) client[0].close(); srv.close(); } catch (Exception ignored) {}
        }));
        while (true) { try { Thread.sleep(3600000); } catch (InterruptedException e) { break; } }
    }
}
