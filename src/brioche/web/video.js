"use strict";
// Decoder-only adapter. Capture, encoding and input permissions stay in Brioche.
window.BriocheVideo = class BriocheVideo {
  constructor({draw, failure, switching}) {
    this.draw = draw; this.failure = failure; this.switching = switching;
    this.decoder = null; this.frames = new Map(); this.serial = 0;
    this.channel = null; this.codec = "mjpeg"; this.setup = Promise.resolve();
  }
  static async supportsH264() {
    if (!window.isSecureContext || !window.VideoDecoder || !window.EncodedVideoChunk) return false;
    try {
      return (await VideoDecoder.isConfigSupported({codec:"avc1.42E01E", optimizeForLatency:true,
        hardwareAcceleration:"no-preference"})).supported;
    } catch (_) { return false; }
  }
  closeDecoder() {
    this.serial++;
    if (this.decoder && this.decoder.state !== "closed") this.decoder.close();
    this.decoder = null; this.frames.clear();
  }
  close() { this.closeDecoder(); this.channel = null; }
  reset(info, channel) {
    this.closeDecoder();
    this.channel = channel; this.codec = info.codec; this.epoch = info.stream_id;
    this.width = info.width; this.height = info.height; this.revision = 0;
    this.setup = Promise.resolve();
  }
  label() { return this.codec === "h264" ? "H.264 · 浏览器解码" : "JPEG · 兼容模式"; }
  ack(channel, id) {
    if (channel.readyState === WebSocket.OPEN) channel.send(JSON.stringify({type:"frame_ack", frame:id}));
  }
  configure(config, channel) {
    if (channel !== this.channel || config.stream_id !== this.epoch || this.codec !== "h264") return;
    this.closeDecoder();
    this.revision = config.revision;
    const serial = this.serial;
    this.setup = (async () => {
      const options = {codec:config.codec, codedWidth:config.width, codedHeight:config.height,
        optimizeForLatency:true, hardwareAcceleration:"no-preference"};
      // No description => Annex B access units with in-band SPS/PPS.
      const supported = await VideoDecoder.isConfigSupported(options);
      if (serial !== this.serial || channel !== this.channel || config.stream_id !== this.epoch) return;
      if (!supported.supported) throw new Error("浏览器不支持当前 H.264 配置");
      this.decoder = new VideoDecoder({
        output:frame => this.output(frame, serial),
        error:() => { if(serial === this.serial) this.failure("H.264 解码失败", channel); }
      });
      this.decoder.configure(supported.config);
    })().catch(() => { if(serial === this.serial) this.failure("浏览器无法使用当前 H.264 配置", channel); });
  }
  output(frame, serial) {
    try {
      if (serial !== this.serial) return;
      const meta = this.frames.get(frame.timestamp);
      if (!meta) return;
      this.frames.delete(frame.timestamp);
      try {
        if (meta.channel === this.channel && meta.epoch === this.epoch && meta.revision === this.revision && !this.switching()) {
          if (frame.displayWidth !== this.width || frame.displayHeight !== this.height) throw new Error("尺寸不符");
          this.draw(frame);
        }
      } finally { this.ack(meta.channel, meta.id); }
    } catch (_) { this.failure("浏览器无法显示 H.264 画面", this.channel); }
    finally { frame.close(); }
  }
  async receive(data, channel) {
    if (channel !== this.channel || data.byteLength < 10) return;
    const header = new DataView(data), epoch = header.getUint32(0), id = header.getUint32(4);
    if (this.codec === "h264") {
      if (data.byteLength < 22) { this.failure("H.264 数据不完整", channel); return; }
      const revision = header.getUint32(8), key = !!header.getUint8(12);
      const timestamp = header.getUint32(13)*4294967296 + header.getUint32(17);
      await this.setup;
      if (channel !== this.channel || epoch !== this.epoch || revision !== this.revision) { this.ack(channel,id); return; }
      try {
        if (!this.decoder || this.decoder.state !== "configured" || this.decoder.decodeQueueSize > 8) throw new Error("解码队列异常");
        this.frames.set(timestamp,{id,channel,epoch,revision});
        // Decode old-screen references while a switch is pending. If the switch
        // fails, the old chain remains valid; output alone is suppressed.
        this.decoder.decode(new EncodedVideoChunk({type:key?"key":"delta",timestamp,data:new Uint8Array(data,21)}));
      } catch (_) {
        this.frames.delete(timestamp); this.ack(channel,id);
        this.failure("H.264 解码不可用",channel);
      }
      return;
    }
    let image = null;
    const serial = this.serial;
    try {
      if (epoch !== this.epoch || this.switching()) return;
      image = await createImageBitmap(new Blob([data.slice(8)],{type:"image/jpeg"}));
      if (channel !== this.channel || epoch !== this.epoch || serial !== this.serial || this.switching()) return;
      if (image.width !== this.width || image.height !== this.height) throw new Error("尺寸不符");
      this.draw(image);
    } catch (_) { this.failure("浏览器无法显示兼容画面",channel); }
    finally { if(image) image.close(); this.ack(channel,id); }
  }
};
