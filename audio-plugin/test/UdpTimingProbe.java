import com.sedmelluq.discord.lavaplayer.udpqueue.natives.UdpQueueManager;
import java.net.*;
import java.nio.ByteBuffer;

/** Operator diagnostic: exercise the actual native sender, on loopback only.
 * Run as a LaunchAgent to include macOS service scheduling in the measurement.
 */
public class UdpTimingProbe {
  public static void main(String[] args) throws Exception {
    UdpQueueManager manager = new UdpQueueManager(300, 20_000_000L, 4096);
    Thread sender = new Thread(manager::process, "gene-udp-timing");
    sender.setDaemon(true);
    sender.start();
    try (DatagramSocket socket = new DatagramSocket(new InetSocketAddress("127.0.0.1", 0))) {
      socket.setSoTimeout(3000);
      ByteBuffer buffer = ByteBuffer.allocateDirect(100);
      buffer.put(new byte[100]).flip();
      for (int i = 0; i < 250; i++) {
        if (!manager.queuePacket(1, buffer, new InetSocketAddress("127.0.0.1", socket.getLocalPort())))
          throw new IllegalStateException("Could not queue diagnostic packet");
      }
      long first = 0, last = 0, maxGap = 0;
      for (int i = 0; i < 250; i++) {
        socket.receive(new DatagramPacket(new byte[4096], 4096));
        long now = System.nanoTime();
        if (i == 0) first = now;
        else maxGap = Math.max(maxGap, now - last);
        last = now;
      }
      double fps = 249e9 / (last - first);
      System.out.printf("NATIVE_UDP fps=%.2f max_gap_ms=%.2f%n", fps, maxGap / 1e6);
      if (fps < 45 || maxGap > 100_000_000L)
        throw new IllegalStateException("Voice sender is missing the 20 ms cadence");
    } finally {
      manager.close();
    }
  }
}
