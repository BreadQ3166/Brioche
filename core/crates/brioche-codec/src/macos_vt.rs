//! In-process native macOS 12.0+ Apple VideoToolbox hardware H.264/HEVC encoder & decoder.
//!
//! Zero external ffmpeg subprocesses, zero rawvideo pipes, true live ABR bitrate updates
//! (`VTSessionSetProperty(AverageBitRate)`), and instant PLI forced IDR keyframes.

use std::ffi::c_void;
use std::ptr;
use std::sync::mpsc::{self, Receiver, SyncSender};

use crate::{CodecError, DecodedYuvFrame, EncodeProfile, EncodedAccessUnit, PixelChroma, VideoCodec};

type OSStatus = i32;
type FourCharCode = u32;
type CFTypeRef = *const c_void;
type CFAllocatorRef = *const c_void;
type CFDictionaryRef = *const c_void;
type CFMutableDictionaryRef = *mut c_void;
type CFStringRef = *const c_void;
type CFNumberRef = *const c_void;
type CFBooleanRef = *const c_void;
type CVPixelBufferRef = *mut c_void;
type CMSampleBufferRef = *mut c_void;
type CMBlockBufferRef = *mut c_void;
type CMFormatDescriptionRef = *const c_void;
type VTCompressionSessionRef = *mut c_void;
type VTDecompressionSessionRef = *mut c_void;

const K_CM_VIDEO_CODEC_H264: FourCharCode = u32::from_be_bytes(*b"avc1");
const K_CM_VIDEO_CODEC_HEVC: FourCharCode = u32::from_be_bytes(*b"hvc1");
const K_CV_PIXEL_FORMAT_32BGRA: FourCharCode = u32::from_be_bytes(*b"BGRA");
const K_CV_PIXEL_FORMAT_420_PLANAR: FourCharCode = u32::from_be_bytes(*b"y420");
const K_CF_NUMBER_SINT32_TYPE: isize = 3;

#[repr(C)]
#[derive(Clone, Copy)]
struct CMTime {
    value: i64,
    timescale: i32,
    flags: u32,
    epoch: i64,
}

impl CMTime {
    const INVALID: Self = Self {
        value: 0,
        timescale: 0,
        flags: 0,
        epoch: 0,
    };

    fn make(value: i64, timescale: i32) -> Self {
        Self {
            value,
            timescale,
            flags: 1, // kCMTimeFlags_Valid
            epoch: 0,
        }
    }
}

#[repr(C)]
struct VTDecompressionOutputCallbackRecord {
    callback: extern "C" fn(*mut c_void, *mut c_void, OSStatus, u32, CVPixelBufferRef, CMTime, CMTime),
    refcon: *mut c_void,
}

#[link(name = "CoreFoundation", kind = "framework")]
extern "C" {
    static kCFAllocatorDefault: CFAllocatorRef;
    static kCFAllocatorNull: CFAllocatorRef;
    static kCFBooleanTrue: CFBooleanRef;
    static kCFBooleanFalse: CFBooleanRef;
    static kCFTypeDictionaryKeyCallBacks: c_void;
    static kCFTypeDictionaryValueCallBacks: c_void;

    fn CFRelease(cf: CFTypeRef);
    fn CFNumberCreate(allocator: CFAllocatorRef, the_type: isize, value_ptr: *const c_void) -> CFNumberRef;
    fn CFDictionaryCreateMutable(
        allocator: CFAllocatorRef,
        capacity: isize,
        key_callbacks: *const c_void,
        value_callbacks: *const c_void,
    ) -> CFMutableDictionaryRef;
    fn CFDictionarySetValue(dict: CFMutableDictionaryRef, key: *const c_void, value: *const c_void);
    fn CFArrayGetCount(array: CFTypeRef) -> isize;
    fn CFArrayGetValueAtIndex(array: CFTypeRef, idx: isize) -> *const c_void;
    fn CFDictionaryContainsKey(dict: CFDictionaryRef, key: *const c_void) -> u8;
    fn CFDictionaryGetValue(dict: CFDictionaryRef, key: *const c_void) -> *const c_void;
}

#[link(name = "CoreVideo", kind = "framework")]
extern "C" {
    fn CVPixelBufferCreate(
        allocator: CFAllocatorRef,
        width: usize,
        height: usize,
        pixel_format_type: FourCharCode,
        pixel_buffer_attributes: CFDictionaryRef,
        pixel_buffer_out: *mut CVPixelBufferRef,
    ) -> OSStatus;
    fn CVPixelBufferRelease(pixel_buffer: CVPixelBufferRef);
    fn CVPixelBufferLockBaseAddress(pixel_buffer: CVPixelBufferRef, lock_flags: u64) -> OSStatus;
    fn CVPixelBufferUnlockBaseAddress(pixel_buffer: CVPixelBufferRef, unlock_flags: u64) -> OSStatus;
    fn CVPixelBufferGetBaseAddress(pixel_buffer: CVPixelBufferRef) -> *mut u8;
    fn CVPixelBufferGetBytesPerRow(pixel_buffer: CVPixelBufferRef) -> usize;
    fn CVPixelBufferGetPlaneCount(pixel_buffer: CVPixelBufferRef) -> usize;
    fn CVPixelBufferGetBaseAddressOfPlane(pixel_buffer: CVPixelBufferRef, plane_index: usize) -> *const u8;
    fn CVPixelBufferGetBytesPerRowOfPlane(pixel_buffer: CVPixelBufferRef, plane_index: usize) -> usize;
    fn CVPixelBufferGetWidthOfPlane(pixel_buffer: CVPixelBufferRef, plane_index: usize) -> usize;
    fn CVPixelBufferGetHeightOfPlane(pixel_buffer: CVPixelBufferRef, plane_index: usize) -> usize;
    static kCVPixelBufferPixelFormatTypeKey: CFStringRef;
}

#[link(name = "CoreMedia", kind = "framework")]
extern "C" {
    static kCMSampleAttachmentKey_NotSync: CFStringRef;

    fn CMSampleBufferGetSampleAttachmentsArray(
        sbuf: CMSampleBufferRef,
        create_if_necessary: u8,
    ) -> CFTypeRef;
    fn CMSampleBufferGetFormatDescription(sbuf: CMSampleBufferRef) -> CMFormatDescriptionRef;
    fn CMSampleBufferGetDataBuffer(sbuf: CMSampleBufferRef) -> CMBlockBufferRef;
    fn CMBlockBufferGetDataPointer(
        the_buffer: CMBlockBufferRef,
        offset: usize,
        length_at_offset_out: *mut usize,
        total_length_out: *mut usize,
        data_pointer_out: *mut *mut u8,
    ) -> OSStatus;
    fn CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
        video_desc: CMFormatDescriptionRef,
        parameter_set_index: usize,
        parameter_set_pointer_out: *mut *const u8,
        parameter_set_size_out: *mut usize,
        parameter_set_count_out: *mut usize,
        nal_unit_header_length_out: *mut i32,
    ) -> OSStatus;
    fn CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(
        video_desc: CMFormatDescriptionRef,
        parameter_set_index: usize,
        parameter_set_pointer_out: *mut *const u8,
        parameter_set_size_out: *mut usize,
        parameter_set_count_out: *mut usize,
        nal_unit_header_length_out: *mut i32,
    ) -> OSStatus;
    fn CMVideoFormatDescriptionCreateFromH264ParameterSets(
        allocator: CFAllocatorRef,
        parameter_set_count: usize,
        parameter_set_pointers: *const *const u8,
        parameter_set_sizes: *const usize,
        nal_unit_header_length: i32,
        format_description_out: *mut CMFormatDescriptionRef,
    ) -> OSStatus;
    fn CMVideoFormatDescriptionCreateFromHEVCParameterSets(
        allocator: CFAllocatorRef,
        parameter_set_count: usize,
        parameter_set_pointers: *const *const u8,
        parameter_set_sizes: *const usize,
        nal_unit_header_length: i32,
        extensions: CFDictionaryRef,
        format_description_out: *mut CMFormatDescriptionRef,
    ) -> OSStatus;
    fn CMBlockBufferCreateWithMemoryBlock(
        structure_allocator: CFAllocatorRef,
        memory_block: *mut c_void,
        block_length: usize,
        block_allocator: CFAllocatorRef,
        custom_block_source: *const c_void,
        offset_to_data: usize,
        data_length: usize,
        flags: u32,
        block_buffer_out: *mut CMBlockBufferRef,
    ) -> OSStatus;
    fn CMSampleBufferCreateReady(
        allocator: CFAllocatorRef,
        data_buffer: CMBlockBufferRef,
        format_description: CMFormatDescriptionRef,
        num_samples: isize,
        num_sample_timing_entries: isize,
        sample_timing_array: *const c_void,
        num_sample_size_entries: isize,
        sample_size_array: *const usize,
        sample_buffer_out: *mut CMSampleBufferRef,
    ) -> OSStatus;
}

#[link(name = "VideoToolbox", kind = "framework")]
extern "C" {
    static kVTCompressionPropertyKey_RealTime: CFStringRef;
    static kVTCompressionPropertyKey_AllowFrameReordering: CFStringRef;
    static kVTCompressionPropertyKey_AverageBitRate: CFStringRef;
    static kVTCompressionPropertyKey_MaxKeyFrameInterval: CFStringRef;
    static kVTEncodeFrameOptionKey_ForceKeyFrame: CFStringRef;

    fn VTCompressionSessionCreate(
        allocator: CFAllocatorRef,
        width: i32,
        height: i32,
        codec_type: FourCharCode,
        encoder_specification: CFDictionaryRef,
        source_image_buffer_attributes: CFDictionaryRef,
        compressed_data_allocator: CFAllocatorRef,
        output_callback: extern "C" fn(*mut c_void, *mut c_void, OSStatus, u32, CMSampleBufferRef),
        output_callback_refcon: *mut c_void,
        compression_session_out: *mut VTCompressionSessionRef,
    ) -> OSStatus;
    fn VTCompressionSessionPrepareToEncodeFrames(session: VTCompressionSessionRef) -> OSStatus;
    fn VTCompressionSessionEncodeFrame(
        session: VTCompressionSessionRef,
        image_buffer: CVPixelBufferRef,
        pts: CMTime,
        duration: CMTime,
        frame_properties: CFDictionaryRef,
        source_frame_refcon: *mut c_void,
        info_flags_out: *mut u32,
    ) -> OSStatus;
    fn VTCompressionSessionCompleteFrames(session: VTCompressionSessionRef, until_pts: CMTime) -> OSStatus;
    fn VTCompressionSessionInvalidate(session: VTCompressionSessionRef);
    fn VTSessionSetProperty(session: *mut c_void, property_key: CFStringRef, property_value: CFTypeRef) -> OSStatus;

    fn VTDecompressionSessionCreate(
        allocator: CFAllocatorRef,
        video_format_description: CMFormatDescriptionRef,
        video_decoder_specification: CFDictionaryRef,
        destination_image_buffer_attributes: CFDictionaryRef,
        output_callback: *const VTDecompressionOutputCallbackRecord,
        decompression_session_out: *mut VTDecompressionSessionRef,
    ) -> OSStatus;
    fn VTDecompressionSessionDecodeFrame(
        session: VTDecompressionSessionRef,
        sample_buffer: CMSampleBufferRef,
        decode_flags: u32,
        source_frame_refcon: *mut c_void,
        info_flags_out: *mut u32,
    ) -> OSStatus;
    fn VTDecompressionSessionWaitForAsynchronousFrames(session: VTDecompressionSessionRef) -> OSStatus;
    fn VTDecompressionSessionInvalidate(session: VTDecompressionSessionRef);
}

struct EncCallbackCtx {
    codec: VideoCodec,
    tx: SyncSender<EncodedAccessUnit>,
}

extern "C" fn vt_encode_output_cb(
    refcon: *mut c_void,
    _source_refcon: *mut c_void,
    status: OSStatus,
    _info_flags: u32,
    sample_buffer: CMSampleBufferRef,
) {
    if status != 0 || sample_buffer.is_null() || refcon.is_null() {
        return;
    }
    unsafe {
        let ctx = &*(refcon as *const EncCallbackCtx);
        let attachments = CMSampleBufferGetSampleAttachmentsArray(sample_buffer, 0);
        let mut is_keyframe = true;
        if !attachments.is_null() && CFArrayGetCount(attachments) > 0 {
            let dict = CFArrayGetValueAtIndex(attachments, 0) as CFDictionaryRef;
            if !dict.is_null() && CFDictionaryContainsKey(dict, kCMSampleAttachmentKey_NotSync as *const c_void) != 0 {
                let not_sync = CFDictionaryGetValue(dict, kCMSampleAttachmentKey_NotSync as *const c_void);
                if not_sync == kCFBooleanTrue as *const c_void {
                    is_keyframe = false;
                }
            }
        }

        let mut annexb = Vec::with_capacity(64 * 1024);
        if is_keyframe {
            let fmt = CMSampleBufferGetFormatDescription(sample_buffer);
            if !fmt.is_null() {
                let mut count: usize = 0;
                let mut nal_hdr: i32 = 4;
                let rc = match ctx.codec {
                    VideoCodec::H264 => CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                        fmt,
                        0,
                        ptr::null_mut(),
                        ptr::null_mut(),
                        &mut count,
                        &mut nal_hdr,
                    ),
                    VideoCodec::Hevc => CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(
                        fmt,
                        0,
                        ptr::null_mut(),
                        ptr::null_mut(),
                        &mut count,
                        &mut nal_hdr,
                    ),
                };
                if rc == 0 {
                    for idx in 0..count {
                        let mut ps_ptr: *const u8 = ptr::null();
                        let mut ps_len: usize = 0;
                        let prc = match ctx.codec {
                            VideoCodec::H264 => CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                                fmt,
                                idx,
                                &mut ps_ptr,
                                &mut ps_len,
                                ptr::null_mut(),
                                ptr::null_mut(),
                            ),
                            VideoCodec::Hevc => CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(
                                fmt,
                                idx,
                                &mut ps_ptr,
                                &mut ps_len,
                                ptr::null_mut(),
                                ptr::null_mut(),
                            ),
                        };
                        if prc == 0 && !ps_ptr.is_null() && ps_len > 0 {
                            annexb.extend_from_slice(&[0, 0, 0, 1]);
                            annexb.extend_from_slice(std::slice::from_raw_parts(ps_ptr, ps_len));
                        }
                    }
                }
            }
        }

        let block = CMSampleBufferGetDataBuffer(sample_buffer);
        if block.is_null() {
            return;
        }
        let mut total_len: usize = 0;
        let mut data_ptr: *mut u8 = ptr::null_mut();
        if CMBlockBufferGetDataPointer(block, 0, ptr::null_mut(), &mut total_len, &mut data_ptr) != 0
            || data_ptr.is_null()
            || total_len < 4
        {
            return;
        }
        let raw = std::slice::from_raw_parts(data_ptr, total_len);
        let mut off = 0usize;
        while off + 4 <= raw.len() {
            let nal_len = u32::from_be_bytes([raw[off], raw[off + 1], raw[off + 2], raw[off + 3]]) as usize;
            off += 4;
            if nal_len == 0 || off + nal_len > raw.len() {
                break;
            }
            annexb.extend_from_slice(&[0, 0, 0, 1]);
            annexb.extend_from_slice(&raw[off..off + nal_len]);
            off += nal_len;
        }
        if !annexb.is_empty() {
            let _ = ctx.tx.try_send(EncodedAccessUnit {
                data: annexb,
                is_idr: is_keyframe,
            });
        }
    }
}

/// In-process Apple VideoToolbox hardware encoder (H.264 / HEVC, zero subprocesses).
pub struct VtEncoder {
    session: VTCompressionSessionRef,
    cb_ctx: Box<EncCallbackCtx>,
    rx: Receiver<EncodedAccessUnit>,
    pub src_width: u32,
    pub src_height: u32,
    pub dst_width: u32,
    pub dst_height: u32,
    pub fps: u32,
    pub codec: VideoCodec,
    pts: i64,
    force_next_idr: bool,
}

unsafe impl Send for VtEncoder {}

impl VtEncoder {
    pub fn start(profile: &EncodeProfile) -> Result<Self, CodecError> {
        let (tx, rx) = mpsc::sync_channel(8);
        let mut cb_ctx = Box::new(EncCallbackCtx {
            codec: profile.codec,
            tx,
        });
        let codec_fourcc = match profile.codec {
            VideoCodec::Hevc => K_CM_VIDEO_CODEC_HEVC,
            VideoCodec::H264 => K_CM_VIDEO_CODEC_H264,
        };
        let mut session: VTCompressionSessionRef = ptr::null_mut();
        unsafe {
            let rc = VTCompressionSessionCreate(
                kCFAllocatorDefault,
                profile.dst_width as i32,
                profile.dst_height as i32,
                codec_fourcc,
                ptr::null(),
                ptr::null(),
                kCFAllocatorDefault,
                vt_encode_output_cb,
                (&mut *cb_ctx) as *mut EncCallbackCtx as *mut c_void,
                &mut session,
            );
            if rc != 0 || session.is_null() {
                return Err(CodecError::Process(format!("VTCompressionSessionCreate failed: {rc}")));
            }
            VTSessionSetProperty(session, kVTCompressionPropertyKey_RealTime, kCFBooleanTrue);
            VTSessionSetProperty(session, kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse);
            let gop = (profile.fps * 5) as i32;
            let gop_num = CFNumberCreate(kCFAllocatorDefault, K_CF_NUMBER_SINT32_TYPE, &gop as *const i32 as *const c_void);
            VTSessionSetProperty(session, kVTCompressionPropertyKey_MaxKeyFrameInterval, gop_num);
            CFRelease(gop_num);

            let bitrate_bps = (profile.bitrate_kbps as i32).saturating_mul(1000);
            let br_num = CFNumberCreate(kCFAllocatorDefault, K_CF_NUMBER_SINT32_TYPE, &bitrate_bps as *const i32 as *const c_void);
            VTSessionSetProperty(session, kVTCompressionPropertyKey_AverageBitRate, br_num);
            CFRelease(br_num);

            let prep = VTCompressionSessionPrepareToEncodeFrames(session);
            if prep != 0 {
                VTCompressionSessionInvalidate(session);
                CFRelease(session as CFTypeRef);
                return Err(CodecError::Process(format!("VTCompressionSessionPrepare failed: {prep}")));
            }
        }
        Ok(Self {
            session,
            cb_ctx,
            rx,
            src_width: profile.src_width,
            src_height: profile.src_height,
            dst_width: profile.dst_width,
            dst_height: profile.dst_height,
            fps: profile.fps.max(1),
            codec: profile.codec,
            pts: 0,
            force_next_idr: true,
        })
    }

    /// Live zero-restart bitrate update driven by TWCC congestion controller.
    pub fn set_bitrate_kbps(&mut self, kbps: u32) {
        let bps = (kbps.clamp(1_500, 100_000) as i32).saturating_mul(1000);
        unsafe {
            let num = CFNumberCreate(kCFAllocatorDefault, K_CF_NUMBER_SINT32_TYPE, &bps as *const i32 as *const c_void);
            VTSessionSetProperty(self.session, kVTCompressionPropertyKey_AverageBitRate, num);
            CFRelease(num);
        }
    }

    /// Request an immediate hardware IDR frame on the next encode call (PLI recovery).
    pub fn force_idr(&mut self) {
        self.force_next_idr = true;
    }

    pub fn push_bgra(&mut self, bgra: &[u8]) -> Result<(), CodecError> {
        let w = self.src_width as usize;
        let h = self.src_height as usize;
        if bgra.len() < w * h * 4 {
            return Err(CodecError::Process("BGRA buffer shorter than frame size".into()));
        }
        unsafe {
            let mut pb: CVPixelBufferRef = ptr::null_mut();
            let rc = CVPixelBufferCreate(
                kCFAllocatorDefault,
                w,
                h,
                K_CV_PIXEL_FORMAT_32BGRA,
                ptr::null(),
                &mut pb,
            );
            if rc != 0 || pb.is_null() {
                return Err(CodecError::Process(format!("CVPixelBufferCreate failed: {rc}")));
            }
            CVPixelBufferLockBaseAddress(pb, 0);
            let dst_base = CVPixelBufferGetBaseAddress(pb);
            let dst_stride = CVPixelBufferGetBytesPerRow(pb);
            let row_bytes = w * 4;
            if dst_stride == row_bytes {
                ptr::copy_nonoverlapping(bgra.as_ptr(), dst_base, row_bytes * h);
            } else {
                for row in 0..h {
                    ptr::copy_nonoverlapping(
                        bgra.as_ptr().add(row * row_bytes),
                        dst_base.add(row * dst_stride),
                        row_bytes,
                    );
                }
            }
            CVPixelBufferUnlockBaseAddress(pb, 0);

            let mut props: CFMutableDictionaryRef = ptr::null_mut();
            if self.force_next_idr {
                self.force_next_idr = false;
                props = CFDictionaryCreateMutable(
                    kCFAllocatorDefault,
                    1,
                    &kCFTypeDictionaryKeyCallBacks,
                    &kCFTypeDictionaryValueCallBacks,
                );
                CFDictionarySetValue(props, kVTEncodeFrameOptionKey_ForceKeyFrame as *const c_void, kCFBooleanTrue as *const c_void);
            }
            let pts = CMTime::make(self.pts, self.fps as i32);
            let dur = CMTime::make(1, self.fps as i32);
            self.pts += 1;
            let erc = VTCompressionSessionEncodeFrame(
                self.session,
                pb,
                pts,
                dur,
                props as CFDictionaryRef,
                ptr::null_mut(),
                ptr::null_mut(),
            );
            if !props.is_null() {
                CFRelease(props as CFTypeRef);
            }
            CVPixelBufferRelease(pb);
            if erc != 0 {
                return Err(CodecError::Process(format!("VTCompressionSessionEncodeFrame failed: {erc}")));
            }
        }
        Ok(())
    }

    pub fn flush(&mut self) {
        unsafe {
            let _ = VTCompressionSessionCompleteFrames(self.session, CMTime::INVALID);
        }
    }

    pub fn try_recv_au(&self) -> Option<EncodedAccessUnit> {
        self.rx.try_recv().ok()
    }

    pub fn recv_au_timeout(&self, timeout: std::time::Duration) -> Option<EncodedAccessUnit> {
        self.rx.recv_timeout(timeout).ok()
    }
}

impl Drop for VtEncoder {
    fn drop(&mut self) {
        unsafe {
            let _ = VTCompressionSessionCompleteFrames(self.session, CMTime::INVALID);
            VTCompressionSessionInvalidate(self.session);
            CFRelease(self.session as CFTypeRef);
        }
        let _ = &self.cb_ctx;
    }
}

struct DecCallbackCtx {
    tx: SyncSender<DecodedYuvFrame>,
}

extern "C" fn vt_decode_output_cb(
    refcon: *mut c_void,
    _source_refcon: *mut c_void,
    status: OSStatus,
    _info_flags: u32,
    image_buffer: CVPixelBufferRef,
    _pts: CMTime,
    _dur: CMTime,
) {
    if status != 0 || image_buffer.is_null() || refcon.is_null() {
        return;
    }
    unsafe {
        let ctx = &*(refcon as *const DecCallbackCtx);
        if CVPixelBufferLockBaseAddress(image_buffer, 1) != 0 {
            return;
        }
        let planes = CVPixelBufferGetPlaneCount(image_buffer);
        if planes >= 3 {
            let w = CVPixelBufferGetWidthOfPlane(image_buffer, 0);
            let h = CVPixelBufferGetHeightOfPlane(image_buffer, 0);
            let uw = CVPixelBufferGetWidthOfPlane(image_buffer, 1);
            let uh = CVPixelBufferGetHeightOfPlane(image_buffer, 1);
            let copy_plane = |idx: usize, pw: usize, ph: usize| -> Vec<u8> {
                let base = CVPixelBufferGetBaseAddressOfPlane(image_buffer, idx);
                let stride = CVPixelBufferGetBytesPerRowOfPlane(image_buffer, idx);
                let mut out = Vec::with_capacity(pw * ph);
                if !base.is_null() {
                    for r in 0..ph {
                        out.extend_from_slice(std::slice::from_raw_parts(base.add(r * stride), pw));
                    }
                }
                out
            };
            let y = copy_plane(0, w, h);
            let u = copy_plane(1, uw, uh);
            let v = copy_plane(2, uw, uh);
            let _ = ctx.tx.try_send(DecodedYuvFrame {
                width: w as u32,
                height: h as u32,
                chroma: PixelChroma::Yuv420p,
                y,
                u,
                v,
            });
        }
        CVPixelBufferUnlockBaseAddress(image_buffer, 1);
    }
}

/// In-process Apple VideoToolbox hardware H.264 / HEVC decoder outputting planar YUV420p.
pub struct VtDecoder {
    codec: VideoCodec,
    session: Option<VTDecompressionSessionRef>,
    fmt_desc: Option<CMFormatDescriptionRef>,
    cb_ctx: Box<DecCallbackCtx>,
    rx: Receiver<DecodedYuvFrame>,
}

unsafe impl Send for VtDecoder {}

impl VtDecoder {
    pub fn start(codec: VideoCodec) -> Result<Self, CodecError> {
        let (tx, rx) = mpsc::sync_channel(8);
        Ok(Self {
            codec,
            session: None,
            fmt_desc: None,
            cb_ctx: Box::new(DecCallbackCtx { tx }),
            rx,
        })
    }

    fn ensure_session(&mut self, param_sets: &[Vec<u8>]) -> Result<(), CodecError> {
        if self.session.is_some() || param_sets.is_empty() {
            return Ok(());
        }
        let ptrs: Vec<*const u8> = param_sets.iter().map(|v| v.as_ptr()).collect();
        let sizes: Vec<usize> = param_sets.iter().map(|v| v.len()).collect();
        let mut fmt: CMFormatDescriptionRef = ptr::null();
        unsafe {
            let rc = match self.codec {
                VideoCodec::H264 => CMVideoFormatDescriptionCreateFromH264ParameterSets(
                    kCFAllocatorDefault,
                    ptrs.len(),
                    ptrs.as_ptr(),
                    sizes.as_ptr(),
                    4,
                    &mut fmt,
                ),
                VideoCodec::Hevc => CMVideoFormatDescriptionCreateFromHEVCParameterSets(
                    kCFAllocatorDefault,
                    ptrs.len(),
                    ptrs.as_ptr(),
                    sizes.as_ptr(),
                    4,
                    ptr::null(),
                    &mut fmt,
                ),
            };
            if rc != 0 || fmt.is_null() {
                return Err(CodecError::Process(format!("CMVideoFormatDescription create failed: {rc}")));
            }
            let dst_attrs = CFDictionaryCreateMutable(
                kCFAllocatorDefault,
                1,
                &kCFTypeDictionaryKeyCallBacks,
                &kCFTypeDictionaryValueCallBacks,
            );
            let pix_fmt = K_CV_PIXEL_FORMAT_420_PLANAR as i32;
            let num = CFNumberCreate(kCFAllocatorDefault, K_CF_NUMBER_SINT32_TYPE, &pix_fmt as *const i32 as *const c_void);
            CFDictionarySetValue(dst_attrs, kCVPixelBufferPixelFormatTypeKey as *const c_void, num as *const c_void);
            CFRelease(num);

            let cb = VTDecompressionOutputCallbackRecord {
                callback: vt_decode_output_cb,
                refcon: (&mut *self.cb_ctx) as *mut DecCallbackCtx as *mut c_void,
            };
            let mut session: VTDecompressionSessionRef = ptr::null_mut();
            let src = VTDecompressionSessionCreate(
                kCFAllocatorDefault,
                fmt,
                ptr::null(),
                dst_attrs as CFDictionaryRef,
                &cb,
                &mut session,
            );
            CFRelease(dst_attrs as CFTypeRef);
            if src != 0 || session.is_null() {
                CFRelease(fmt as CFTypeRef);
                return Err(CodecError::Process(format!("VTDecompressionSessionCreate failed: {src}")));
            }
            self.fmt_desc = Some(fmt);
            self.session = Some(session);
        }
        Ok(())
    }

    pub fn push_access_unit(&mut self, au: &[u8]) -> Result<(), CodecError> {
        let mut param_sets: Vec<Vec<u8>> = Vec::new();
        let mut avcc = Vec::with_capacity(au.len() + 16);
        let starts = crate::find_start_codes(au);
        for (i, &start) in starts.iter().enumerate() {
            let end = starts.get(i + 1).copied().unwrap_or(au.len());
            let sc_len = if au[start..end].starts_with(&[0, 0, 0, 1]) { 4 } else { 3 };
            if end <= start + sc_len {
                continue;
            }
            let nal = &au[start + sc_len..end];
            let is_param = match self.codec {
                VideoCodec::H264 => matches!(nal[0] & 0x1F, 7 | 8),
                VideoCodec::Hevc => matches!((nal[0] >> 1) & 0x3F, 32 | 33 | 34),
            };
            if is_param {
                param_sets.push(nal.to_vec());
            } else {
                avcc.extend_from_slice(&(nal.len() as u32).to_be_bytes());
                avcc.extend_from_slice(nal);
            }
        }
        if !param_sets.is_empty() && self.session.is_none() {
            self.ensure_session(&param_sets)?;
        }
        let (Some(session), Some(fmt)) = (self.session, self.fmt_desc) else {
            return Ok(());
        };
        if avcc.is_empty() {
            return Ok(());
        }
        unsafe {
            let mut block: CMBlockBufferRef = ptr::null_mut();
            // CRITICAL: pass kCFAllocatorNull so CoreMedia does NOT call free() on Rust's Vec<u8>!
            let rc = CMBlockBufferCreateWithMemoryBlock(
                kCFAllocatorDefault,
                avcc.as_mut_ptr() as *mut c_void,
                avcc.len(),
                kCFAllocatorNull,
                ptr::null(),
                0,
                avcc.len(),
                0,
                &mut block,
            );
            if rc != 0 || block.is_null() {
                return Err(CodecError::Process(format!("CMBlockBuffer failed: {rc}")));
            }
            let sample_size = avcc.len();
            let mut sbuf: CMSampleBufferRef = ptr::null_mut();
            let src = CMSampleBufferCreateReady(
                kCFAllocatorDefault,
                block,
                fmt,
                1,
                0,
                ptr::null(),
                1,
                &sample_size,
                &mut sbuf,
            );
            CFRelease(block as CFTypeRef);
            if src != 0 || sbuf.is_null() {
                return Err(CodecError::Process(format!("CMSampleBufferCreateReady failed: {src}")));
            }
            let drc = VTDecompressionSessionDecodeFrame(session, sbuf, 0, ptr::null_mut(), ptr::null_mut());
            let _ = VTDecompressionSessionWaitForAsynchronousFrames(session);
            CFRelease(sbuf as CFTypeRef);
            if drc != 0 {
                return Err(CodecError::Process(format!("VTDecompressionSessionDecodeFrame failed: {drc}")));
            }
        }
        Ok(())
    }

    pub fn recv_frame_timeout(&self, timeout: std::time::Duration) -> Option<DecodedYuvFrame> {
        self.rx.recv_timeout(timeout).ok()
    }
}

impl Drop for VtDecoder {
    fn drop(&mut self) {
        unsafe {
            if let Some(s) = self.session.take() {
                VTDecompressionSessionInvalidate(s);
                CFRelease(s as CFTypeRef);
            }
            if let Some(f) = self.fmt_desc.take() {
                CFRelease(f as CFTypeRef);
            }
        }
    }
}
