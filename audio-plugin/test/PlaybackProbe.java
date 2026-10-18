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
    manager.registerSourceManager(new gene.audio.GeneYoutubeSource(args[1], System.getenv("GENE_RESOLVER_TOKEN")));
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
      AudioPlayer player = manager.createPlayer();
      AtomicReference<FriendlyException> error = new AtomicReference<>();
      player.addListener(new AudioEventAdapter() {
        @Override public void onTrackException(AudioPlayer p, AudioTrack t, FriendlyException e) { error.set(e); }
      });
      player.startTrack(track, false);
      long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(90);
      long frames = 0, bytes = 0, nonzero = 0;
      while (frames < 1500 && System.nanoTime() < deadline) {
        if (error.get() != null) throw error.get();
        AudioFrame frame = player.provide();
        if (frame == null) { Thread.sleep(10); continue; }
        if (frame.isTerminator()) break;
        byte[] data = frame.getData();
        frames++; bytes += data.length;
        for (byte b : data) if (b != 0) nonzero++;
      }
      if (track.getDuration() > 65000) {
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
