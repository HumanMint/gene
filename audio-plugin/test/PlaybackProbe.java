import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicReference;
import com.sedmelluq.discord.lavaplayer.player.*;
import com.sedmelluq.discord.lavaplayer.player.event.AudioEventAdapter;
import com.sedmelluq.discord.lavaplayer.tools.FriendlyException;
import com.sedmelluq.discord.lavaplayer.track.*;
import com.sedmelluq.discord.lavaplayer.track.playback.AudioFrame;
import com.sedmelluq.discord.lavaplayer.format.StandardAudioDataFormats;

public class PlaybackProbe {
  public static void main(String[] args) throws Exception {
    DefaultAudioPlayerManager manager = new DefaultAudioPlayerManager();
    manager.getConfiguration().setOutputFormat(StandardAudioDataFormats.DISCORD_PCM_S16_LE);
    manager.registerSourceManager(new gene.audio.GeneYoutubeSource(args[1], System.getenv("GENE_RESOLVER_TOKEN"), System.getenv("GENE_AUDIO_CACHE")));
    CompletableFuture<AudioTrack> loaded = new CompletableFuture<>();
    manager.loadItem(args[0], new AudioLoadResultHandler() {
      public void trackLoaded(AudioTrack track) { loaded.complete(track); }
      public void playlistLoaded(AudioPlaylist playlist) { loaded.complete(playlist.getTracks().get(0)); }
      public void noMatches() { loaded.completeExceptionally(new Exception("No matches")); }
      public void loadFailed(FriendlyException e) { loaded.completeExceptionally(e); }
    });
    try {
      AudioTrack track = loaded.get(30, TimeUnit.SECONDS);
      System.out.println("LOADED " + track.getIdentifier());
      boolean realtime = args.length > 2 && args[2].equals("realtime");
      manager.setFrameBufferDuration(1000);
      AudioPlayer player = manager.createPlayer();
      AtomicReference<FriendlyException> error = new AtomicReference<>();
      AtomicReference<AudioTrackEndReason> ended = new AtomicReference<>();
      player.addListener(new AudioEventAdapter() {
        @Override public void onTrackException(AudioPlayer p, AudioTrack t, FriendlyException e) { error.set(e); }
        @Override public void onTrackEnd(AudioPlayer p, AudioTrack t, AudioTrackEndReason reason) { ended.set(reason); }
      });
      player.startTrack(track, false);
      long started = System.nanoTime();
      long deadline = started + TimeUnit.SECONDS.toNanos(realtime ? 300 : 90);
      long frames = 0, bytes = 0, nonzero = 0;
      long underruns = 0, gap = 0, maxGap = 0, tick = started;
      long target = realtime ? Long.MAX_VALUE : 1500;
      while (frames < target && System.nanoTime() < deadline) {
        if (realtime) {
          long wait = tick - System.nanoTime();
          if (wait > 0) TimeUnit.NANOSECONDS.sleep(wait);
          tick += TimeUnit.MILLISECONDS.toNanos(20);
        }
        if (error.get() != null) throw error.get();
        AudioFrame frame = player.provide();
        if (frame == null) {
          if (ended.get() != null) break;
          if (frames > 0) { underruns++; gap++; maxGap = Math.max(maxGap, gap); }
          if (!realtime) Thread.sleep(10);
          continue;
        }
        if (frame.isTerminator()) break;
        if (frames == 0) System.out.println("STARTUP_MS " + TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started));
        gap = 0;
        byte[] data = frame.getData();
        frames++; bytes += data.length;
        for (byte b : data) if (b != 0) nonzero++;
        if (realtime && frames % 1500 == 0)
          System.out.printf("PROGRESS seconds=%d underruns=%d max_gap_ms=%d%n", frames / 50, underruns, maxGap * 20);
      }
      if (realtime) {
        System.out.printf("REALTIME underruns=%d max_gap_ms=%d elapsed_ms=%d ended=%s%n",
            underruns, maxGap * 20, TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started), ended.get());
        if (ended.get() != AudioTrackEndReason.FINISHED || frames * 20 < track.getDuration() - 1000)
          throw new Exception("Track did not finish completely");
        if (underruns > 5) throw new Exception("Playback ran out of buffered audio");
      }
      if (!realtime && track.getDuration() > 65000) {
        track.setPosition(60000);
        long seekDeadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
        boolean sought = false;
        while (System.nanoTime() < seekDeadline) {
          if (error.get() != null) throw error.get();
          AudioFrame seekFrame = player.provide();
          if (seekFrame != null && seekFrame.getTimecode() >= 60000) { sought = true; break; }
          Thread.sleep(10);
        }
        if (!sought) throw new Exception("Seeking failed");
        System.out.println("SEEK_OK 60000");
      }
      player.destroy();
      System.out.printf("DECODED frames=%d pcm_bytes=%d nonzero_bytes=%d%n", frames, bytes, nonzero);
      if (frames < 250 || nonzero < 1000) throw new Exception("Insufficient decoded audio");
    } finally { manager.shutdown(); }
  }
}
