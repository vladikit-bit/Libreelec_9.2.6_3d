#!/usr/bin/env python3
"""
generate_patches.py v4.12 - Packed Y+UV into ONE combined RGB texture instead of two
                             separate textures, to reduce extra texture reads from 3
                             (depth+Y+UV) back to 2 (depth+packed), matching the read
                             count already confirmed fast in dep_test3.

Test history (all confirmed via [Stereo3D-TOTAL] + observed FPS):
  dep_test3 (1 extra plain read, trivial math):millisecond ~8ms TOTAL, FPS recovers fully.
  nv12_shader/depTest=5 (3 extra reads: depth+Y+UV, full YUV+Dubois math): ~11ms TOTAL,
    720p ~20-22fps (down from ~30), 1080p stuck ~12fps.
  nv12_trivial/depTest=6 (2 extra reads: Y+UV only, NO depth/shift, trivial math):
    ~10ms TOTAL, 720p ~29-30fps (full recovery!), 1080p ~12-17fps (partial).
  This confirms two separate, additive cost contributors: extra read COUNT, and
  math complexity - both scale with resolution.

This version (depTest=7, /storage/.config/stereo3d_nv12_packed): upsamples the UV
plane to Y's resolution on CPU (cheap - 320x180 buffer, plain scalar loop) and packs
Y+U+V into a single RGB texture, read ONCE per fragment (plus one depth read for the
shift = 2 extra reads total, matching dep_test3/dep_test6's proven-lighter read count).
Expectation set going in: 720p should recover close to fully; 1080p may improve but
is not guaranteed to reach full ~24fps, since even 2 reads alone (dep_test6, zero
math) didn't fully recover 1080p - real YUV conversion + shift math sits on top of
that baseline.
"""
import os, sys, difflib

CLEAN    = "build.LibreELEC-clean/kodi-rockchip_18.9-Leia"
OUT_DIR  = "packages/mediacenter/kodi/patches/rockchip"
OUT_FILE = os.path.join(OUT_DIR, "001-stereo3d.patch")

def read_file(rel):
    path = os.path.join(CLEAN, rel)
    if not os.path.exists(path):
        print(f"[ERROR] Not found: {path}")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return f.read()

def replace_once(src, old, new, label):
    if old not in src:
        print(f"[ERROR] Anchor missing: {label}")
        sys.exit(1)
    open_delta  = new.count("{") - old.count("{")
    close_delta = new.count("}") - old.count("}")
    if open_delta != close_delta:
        print(f"[ERROR] BRACE MISMATCH in '{label}': added {{ count={open_delta}, }} count={close_delta}. "
              f"Aborting before writing a broken patch.")
        sys.exit(1)
    return src.replace(old, new, 1)

def make_diff(rel, original, modified):
    orig_lines = original.splitlines(keepends=True)
    mod_lines  = modified.splitlines(keepends=True)
    diff = list(difflib.unified_diff(orig_lines, mod_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    print(f"[+] {rel}" if diff else f"[-] {rel} (no changes)")
    return "".join(diff)

patches = []

# 1. LinuxRendererGLES.h
REL  = "xbmc/cores/VideoPlayer/VideoRenderers/LinuxRendererGLES.h"
orig = read_file(REL)
mod  = replace_once(orig,
    "CRenderSystemGLES *m_renderSystem{nullptr};",
    "CRenderSystemGLES *m_renderSystem{nullptr};\n"
    "  bool m_stereo3d_enabled{false};\n"
    "  int m_stereoMode{0};\n"
    "  unsigned int m_anaDepthTex{0};\n"
    "  unsigned char *m_depthMap{nullptr};\n"
    "  unsigned char *m_depthAccum{nullptr};\n"
    "  unsigned char *m_depthPrev{nullptr};\n"
    "  int m_depthW{0}, m_depthH{0};",
    "GLES.h")
patches.append(make_diff(REL, orig, mod))

# 2. LinuxRendererGLES.cpp (software path - unchanged)
REL  = "xbmc/cores/VideoPlayer/VideoRenderers/LinuxRendererGLES.cpp"
orig = read_file(REL)
src  = orig
src = replace_once(src,
    '#include "VideoShaders/VideoFilterShaderGLES.h"',
    '#include "VideoShaders/VideoFilterShaderGLES.h"\n#include <arm_neon.h>\n#include <unistd.h>',
    "includes")
src = replace_once(src,
    "m_clearColour = CServiceBroker::GetWinSystem()->UseLimitedColor() ? (16.0f / 0xff) : 0.0f;",
    "m_clearColour = CServiceBroker::GetWinSystem()->UseLimitedColor() ? (16.0f / 0xff) : 0.0f;\n"
    "  if (access(\"/storage/.config/stereo3d_color\", F_OK) == 0) m_stereo3d_enabled = true, m_stereoMode = 2;\n"
    "  else if (access(\"/storage/.config/stereo3d_dubois\", F_OK) == 0) m_stereo3d_enabled = true, m_stereoMode = 1;\n"
    "  else if (access(\"/storage/.config/stereo3d\", F_OK) == 0) m_stereo3d_enabled = true, m_stereoMode = 0;\n"
    "  else m_stereo3d_enabled = false;",
    "Config")
src = replace_once(src,
    "free(m_planeBuffer);\n  m_planeBuffer = nullptr;",
    "free(m_planeBuffer);\n  m_planeBuffer = nullptr;\n"
    "  free(m_depthMap); m_depthMap = nullptr;\n"
    "  free(m_depthAccum); m_depthAccum = nullptr;\n"
    "  free(m_depthPrev); m_depthPrev = nullptr;\n"
    "  if (m_anaDepthTex) { glDeleteTextures(1, &m_anaDepthTex); m_anaDepthTex = 0; }",
    "Destruct")
src = replace_once(src,
    "    ret = UploadYV12Texture(index);\n  }\n\n  if (ret)\n",
    "    ret = UploadYV12Texture(index);\n  }\n"
    "  if (ret && m_stereo3d_enabled && (m_format == AV_PIX_FMT_NV12 || m_format == AV_PIX_FMT_YUV420P)) {\n"
    "    YuvImage *im = &m_buffers[index].image;\n"
    "    if (m_currentField == FIELD_FULL && im->plane[0]) {\n"
    "      int dw = im->width / 2, dh = im->height / 2;\n"
    "      if (m_depthW != dw || m_depthH != dh || !m_depthMap) {\n"
    "        free(m_depthMap); free(m_depthAccum); free(m_depthPrev);\n"
    "        m_depthMap = (unsigned char*)calloc(dw * dh, 1);\n"
    "        m_depthAccum = (unsigned char*)calloc(dw * dh, 1);\n"
    "        m_depthPrev = (unsigned char*)calloc((size_t)im->stride[0] * im->height, 1);\n"
    "        m_depthW = dw; m_depthH = dh;\n"
    "        if (!m_anaDepthTex) glGenTextures(1, &m_anaDepthTex);\n"
    "        glBindTexture(GL_TEXTURE_2D, m_anaDepthTex);\n"
    "        glTexImage2D(GL_TEXTURE_2D, 0, GL_LUMINANCE, dw, dh, 0, GL_LUMINANCE, GL_UNSIGNED_BYTE, NULL);\n"
    "        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);\n"
    "        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);\n"
    "      }\n"
    "      for (int y = 0; y < dh; y++) {\n"
    "        uint8_t gv = (uint8_t)((y * 180) / dh);\n"
    "        uint8x16_t v_grad = vdupq_n_u8(gv);\n"
    "        uint8x16_t v_black = vdupq_n_u8(15); uint8x16_t v_white = vdupq_n_u8(235);\n"
    "        unsigned char *rc = (unsigned char*)im->plane[0] + y * 2 * im->stride[0];\n"
    "        unsigned char *rp = m_depthPrev + y * 2 * im->stride[0];\n"
    "        unsigned char *ro = m_depthMap + y * dw;\n"
    "        unsigned char *ra = m_depthAccum + y * dw;\n"
    "        int x = 0;\n"
    "        for (; x <= dw - 16; x += 16) {\n"
    "          uint8x16_t c = vld1q_u8(rc + x * 2);\n"
    "          uint8x16_t p = vld1q_u8(rp + x * 2);\n"
    "          uint8x16_t motion = vabdq_u8(c, p);\n"
    "          uint8x16_t is_text = vorrq_u8(vcleq_u8(c, v_black), vcgeq_u8(c, v_white));\n"
    "          motion = vandq_u8(motion, vmvnq_u8(is_text));\n"
    "          uint8x16_t raw = vqaddq_u8(v_grad, vshrq_n_u8(motion, 1));\n"
    "          uint8x16_t old_a = vld1q_u8(ra + x);\n"
    "          uint8x16_t smooth = vrhaddq_u8(vrhaddq_u8(old_a, old_a), raw);\n"
    "          vst1q_u8(ra + x, smooth); vst1q_u8(ro + x, smooth);\n"
    "        }\n"
    "        for (; x < dw; x++) {\n"
    "          int cv = rc[x*2], pv = rp[x*2];\n"
    "          int m = abs(cv - pv); if (cv < 15 || cv > 235) m = 0;\n"
    "          int raw = gv + (m >> 1); if (raw > 255) raw = 255;\n"
    "          int sm = (ra[x] * 3 + raw) >> 2;\n"
    "          ra[x] = ro[x] = (unsigned char)sm;\n"
    "        }\n"
    "      }\n"
    "      memcpy(m_depthPrev, im->plane[0], (size_t)im->stride[0] * im->height);\n"
    "      glBindTexture(GL_TEXTURE_2D, m_anaDepthTex);\n"
    "      glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, dw, dh, GL_LUMINANCE, GL_UNSIGNED_BYTE, m_depthMap);\n"
    "      glBindTexture(GL_TEXTURE_2D, 0);\n"
    "    }\n"
    "  }\n"
    "  if (ret)\n",
    "NEON")
src = replace_once(src,
    "pYUVShader->SetMatrices(glMatrixProject.Get(), glMatrixModview.Get());\n  pYUVShader->Enable();",
    "pYUVShader->SetMatrices(glMatrixProject.Get(), glMatrixModview.Get());\n"
    "  pYUVShader->SetStereo3D(m_stereo3d_enabled);\n"
    "  pYUVShader->SetStereoMode(m_stereoMode);\n"
    "  if (m_stereo3d_enabled && m_anaDepthTex) {\n"
    "    glActiveTexture(GL_TEXTURE3); glBindTexture(GL_TEXTURE_2D, m_anaDepthTex); glActiveTexture(GL_TEXTURE0);\n"
    "  }\n"
    "  pYUVShader->Enable();",
    "Bind")
patches.append(make_diff(REL, orig, src))

# 3 & 4. YUV2RGBShaderGLES (unchanged)
REL  = "xbmc/cores/VideoPlayer/VideoRenderers/VideoShaders/YUV2RGBShaderGLES.h"
orig = read_file(REL)
src  = replace_once(orig, "void SetToneMapParam(float param) { m_toneMappingParam = param; }",
    "void SetToneMapParam(float param) { m_toneMappingParam = param; }\n    void SetStereo3D(bool e) { m_stereo3d = e; }\n    void SetStereoMode(int m) { m_stereoMode = m; }", "H1")
src  = replace_once(src, "bool m_convertFullRange;\n  };",
    "bool m_convertFullRange;\n    bool m_stereo3d{false}; int m_stereoMode{0};\n    int m_hStereo3D{-1}; int m_hStereoMode{-1}; int m_hSampDepth{-1};\n  };", "H2")
patches.append(make_diff(REL, orig, src))

REL  = "xbmc/cores/VideoPlayer/VideoRenderers/VideoShaders/YUV2RGBShaderGLES.cpp"
orig = read_file(REL)
src  = replace_once(orig, 'm_hToneP1 = glGetUniformLocation(ProgramHandle(), "m_toneP1");\n  VerifyGLState();',
    'm_hToneP1    = glGetUniformLocation(ProgramHandle(), "m_toneP1");\n  m_hStereo3D   = glGetUniformLocation(ProgramHandle(), "u_stereo3d");\n  m_hStereoMode = glGetUniformLocation(ProgramHandle(), "u_stereoMode");\n  m_hSampDepth  = glGetUniformLocation(ProgramHandle(), "m_sampDepth");\n  VerifyGLState();', "C1")
src  = replace_once(src, "  if (m_toneMapping)\n  {",
    "  { glUniform1i(m_hStereo3D, m_stereo3d ? 1 : 0); glUniform1i(m_hStereoMode, m_stereoMode); glUniform1i(m_hSampDepth, 3); }\n  if (m_toneMapping)\n  {", "C2")
patches.append(make_diff(REL, orig, src))

# 5. gles_yuv2rgb_basic.frag (unchanged)
REL  = "system/shaders/GLES/2.0/gles_yuv2rgb_basic.frag"
orig = read_file(REL)
src  = replace_once(orig, "void main()\n{\n  vec4 rgb;\n  vec4 yuv;",
    "uniform int u_stereo3d;\nuniform int u_stereoMode;\nuniform sampler2D m_sampDepth;\nvoid main()\n{\n  vec4 rgb;\n  vec4 yuv;", "F1")
src = replace_once(src, "  gl_FragColor = rgb;\n}",
    "  if (u_stereo3d == 1) {\n"
    "    float d = texture2D(m_sampDepth, m_cordY).r;\n"
    "    float s = d * 0.018;\n"
    "    vec4 yuv_l; yuv_l.r = texture2D(m_sampY, m_cordY - vec2(s, 0.0)).r;\n"
    "    yuv_l.g = texture2D(m_sampU, m_cordY - vec2(s, 0.0)).r - 0.5;\n"
    "    yuv_l.b = texture2D(m_sampV, m_cordY - vec2(s, 0.0)).r - 0.5; yuv_l.a = 1.0;\n"
    "    vec3 L = vec3(yuv_l.r + 1.402*yuv_l.b, yuv_l.r - 0.344*yuv_l.g - 0.714*yuv_l.b, yuv_l.r + 1.772*yuv_l.g);\n"
    "    vec3 R = rgb.rgb;\n"
    "    if (u_stereoMode == 1) gl_FragColor = vec4(clamp(0.437*L.r+0.449*L.g+0.164*L.b-0.011*R.r-0.032*R.g-0.007*R.b,0.0,1.0), clamp(-0.062*L.r-0.062*L.g-0.024*L.b+0.377*R.r+0.761*R.g+0.009*R.b,0.0,1.0), clamp(-0.048*L.r-0.050*L.g-0.017*L.b-0.026*R.r-0.093*R.g+1.234*R.b,0.0,1.0), 1.0);\n"
    "    else if (u_stereoMode == 2) gl_FragColor = vec4(L.r * 1.05, R.g * 0.97, R.b * 0.97, 1.0);\n"
    "    else { float luma = dot(L, vec3(0.299, 0.587, 0.114)); gl_FragColor = vec4(luma, clamp(R.g - luma*0.12, 0.0, 1.0), clamp(R.b - luma*0.12, 0.0, 1.0), 1.0); }\n"
    "  } else gl_FragColor = rgb;\n}", "F2")
patches.append(make_diff(REL, orig, src))

# 6. RendererDRMPRIMEGLES.h (unchanged)
REL  = "xbmc/cores/VideoPlayer/VideoRenderers/HwDecRender/RendererDRMPRIMEGLES.h"
orig = read_file(REL)
patches.append(make_diff(REL, orig, orig))

# 7. RendererDRMPRIMEGLES.cpp
REL  = "xbmc/cores/VideoPlayer/VideoRenderers/HwDecRender/RendererDRMPRIMEGLES.cpp"
orig = read_file(REL)
src  = orig

src = replace_once(src,
    '#include "RendererDRMPRIMEGLES.h"',
    '#include "RendererDRMPRIMEGLES.h"\n'
    '#include <unistd.h>\n'
    '#include <fcntl.h>\n'
    '#include <sys/mman.h>\n'
    '#include <sys/ioctl.h>\n'
    '#include <sys/stat.h>\n'
    '#include <arm_neon.h>\n'
    '#include <chrono>\n'
    '#include <cerrno>\n'
    '#include <cstdint>\n'
    '#include <cstdlib>\n'
    '#include <cstring>\n'
    'struct dma_buf_sync { unsigned long long flags; };\n'
    '#define DMA_BUF_SYNC_READ  (1 << 0)\n'
    '#define DMA_BUF_SYNC_WRITE (2 << 0)\n'
    '#define DMA_BUF_SYNC_START (0 << 2)\n'
    '#define DMA_BUF_SYNC_END   (1 << 2)\n'
    '#define DMA_BUF_IOCTL_SYNC _IOW(\'b\', 0, struct dma_buf_sync)\n'
    '#define STEREO3D_RGA2_BLIT_SYNC 0x6017\n'
    '#define STEREO3D_DRM_IOCTL_BASE \'d\'\n'
    'struct stereo3d_drm_mode_create_dumb { uint32_t height; uint32_t width; uint32_t bpp; uint32_t flags; uint32_t handle; uint32_t pitch; uint64_t size; };\n'
    'struct stereo3d_drm_mode_map_dumb { uint32_t handle; uint32_t pad; uint64_t offset; };\n'
    'struct stereo3d_drm_prime_handle { uint32_t handle; uint32_t flags; int32_t fd; };\n'
    'struct stereo3d_drm_mode_destroy_dumb { uint32_t handle; };\n'
    '#define STEREO3D_DRM_IOCTL_MODE_CREATE_DUMB   _IOWR(STEREO3D_DRM_IOCTL_BASE, 0xB2, struct stereo3d_drm_mode_create_dumb)\n'
    '#define STEREO3D_DRM_IOCTL_MODE_MAP_DUMB      _IOWR(STEREO3D_DRM_IOCTL_BASE, 0xB3, struct stereo3d_drm_mode_map_dumb)\n'
    '#define STEREO3D_DRM_IOCTL_MODE_DESTROY_DUMB  _IOWR(STEREO3D_DRM_IOCTL_BASE, 0xB4, struct stereo3d_drm_mode_destroy_dumb)\n'
    '#define STEREO3D_DRM_IOCTL_PRIME_HANDLE_TO_FD _IOWR(STEREO3D_DRM_IOCTL_BASE, 0x2d, struct stereo3d_drm_prime_handle)\n'
    'typedef struct {\n'
    '  uint64_t yrgb_addr; uint64_t uv_addr; uint64_t v_addr;\n'
    '  uint32_t format; uint16_t act_w; uint16_t act_h;\n'
    '  uint16_t x_offset; uint16_t y_offset; uint16_t vir_w; uint16_t vir_h;\n'
    '  uint16_t endian_mode; uint16_t alpha_swap; uint32_t pad;\n'
    '} S3D_RgaImg;\n'
    'typedef struct {\n'
    '  uint64_t src0_base_addr; uint64_t src1_base_addr;\n'
    '  uint64_t dst_base_addr; uint64_t els_base_addr;\n'
    '  uint8_t src0_mmu_flag; uint8_t src1_mmu_flag;\n'
    '  uint8_t dst_mmu_flag; uint8_t els_mmu_flag; uint8_t pad5[4];\n'
    '} S3D_MmuInfo;\n'
    'typedef struct { short gr_x_a; short gr_y_a; short gr_x_b; short gr_y_b; short gr_x_g; short gr_y_g; short gr_x_r; short gr_y_r; } S3D_ColorFill;\n'
    'struct S3D_Rga2Req {\n'
    '  uint8_t render_mode; uint8_t pad0[7];\n'
    '  S3D_RgaImg src; S3D_RgaImg src1;\n'
    '  S3D_RgaImg dst; S3D_RgaImg pat;\n'
    '  uint64_t rop_mask_addr; uint64_t LUT_addr;\n'
    '  uint32_t rop_mask_stride; uint8_t bitblt_mode; uint8_t rotate_mode;\n'
    '  uint16_t alpha_rop_flag; uint16_t alpha_mode_0; uint16_t alpha_mode_1;\n'
    '  uint8_t scale_bicu_mode; uint8_t pad1[3];\n'
    '  uint32_t color_key_max; uint32_t color_key_min;\n'
    '  uint32_t fg_color; uint32_t bg_color;\n'
    '  uint8_t color_fill_mode; uint8_t pad2[1];\n'
    '  S3D_ColorFill gr_color;\n'
    '  uint8_t fading_alpha_value; uint8_t fading_r_value;\n'
    '  uint8_t fading_g_value; uint8_t fading_b_value;\n'
    '  uint8_t src_a_global_val; uint8_t dst_a_global_val;\n'
    '  uint8_t rop_mode; uint8_t pad3[1]; uint16_t rop_code;\n'
    '  uint8_t palette_mode; uint8_t yuv2rgb_mode;\n'
    '  uint8_t endian_mode; uint8_t CMD_fin_int_enable;\n'
    '  S3D_MmuInfo mmu_info;\n'
    '  uint8_t alpha_zero_key; uint8_t src_trans_mode;\n'
    '  uint8_t alpha_swp; uint8_t dither_mode;\n'
    '  uint8_t rgb2yuv_mode; uint8_t buf_type; uint8_t pad4[2];\n'
    '  uint64_t sg_src0; uint64_t sg_src1; uint64_t sg_dst; uint64_t sg_els;\n'
    '  uint64_t attach_src0; uint64_t attach_src1; uint64_t attach_dst;\n'
    '};\n'
    '#include "cores/VideoPlayer/Process/gbm/VideoBufferDRMPRIME.h"\n'
    '#include "utils/log.h"\n'
    '#define MAX_SHIFT 0.018f\n'
    'static GLuint Stereo3D_CompileShader(GLenum type, const char* src) {\n'
    '  GLuint sh = glCreateShader(type);\n'
    '  glShaderSource(sh, 1, &src, nullptr);\n'
    '  glCompileShader(sh);\n'
    '  GLint ok = 0;\n'
    '  glGetShaderiv(sh, GL_COMPILE_STATUS, &ok);\n'
    '  if (!ok) {\n'
    '    char log[512]; glGetShaderInfoLog(sh, 512, nullptr, log);\n'
    '    CLog::Log(LOGERROR, "[Stereo3D-FX] Shader compile failed: %s", log);\n'
    '    glDeleteShader(sh); return 0;\n'
    '  }\n'
    '  return sh;\n'
    '}\n'
    'static GLuint Stereo3D_LinkProgram(const char* vsSrc, const char* fsSrc) {\n'
    '  GLuint vs = Stereo3D_CompileShader(GL_VERTEX_SHADER, vsSrc);\n'
    '  GLuint fs = Stereo3D_CompileShader(GL_FRAGMENT_SHADER, fsSrc);\n'
    '  if (!vs || !fs) return 0;\n'
    '  GLuint prog = glCreateProgram();\n'
    '  glAttachShader(prog, vs); glAttachShader(prog, fs);\n'
    '  glLinkProgram(prog);\n'
    '  GLint ok = 0;\n'
    '  glGetProgramiv(prog, GL_LINK_STATUS, &ok);\n'
    '  glDeleteShader(vs); glDeleteShader(fs);\n'
    '  if (!ok) {\n'
    '    char log[512]; glGetProgramInfoLog(prog, 512, nullptr, log);\n'
    '    CLog::Log(LOGERROR, "[Stereo3D-FX] Program link failed: %s", log);\n'
    '    glDeleteProgram(prog); return 0;\n'
    '  }\n'
    '  return prog;\n'
    '}\n'
    'static const char* Stereo3D_FxVS =\n'
    '  "attribute vec2 a_pos;\\n"\n'
    '  "varying vec2 v_uv;\\n"\n'
    '  "void main(){ v_uv = a_pos*0.5+0.5; gl_Position = vec4(a_pos,0.0,1.0); }\\n";\n'
    'static const char* Stereo3D_FxFS =\n'
    '  "#extension GL_OES_EGL_image_external : require\\n"\n'
    '  "precision mediump float;\\n"\n'
    '  "uniform samplerExternalOES u_srcTex;\\n"\n'
    '  "uniform sampler2D u_depthTex;\\n"\n'
    '  "uniform float u_shift;\\n"\n'
    '  "uniform int u_mode;\\n"\n'
    '  "varying vec2 v_uv;\\n"\n'
    '  "void main(){\\n"\n'
    '  "  vec3 L = texture2D(u_srcTex, v_uv).rgb;\\n"\n'
    '  "  float d = texture2D(u_depthTex, v_uv).r;\\n"\n'
    '  "  float s = d * u_shift;\\n"\n'
    '  "  vec3 R = texture2D(u_srcTex, clamp(v_uv + vec2(s, 0.0), 0.001, 0.999)).rgb;\\n"\n'
    '  "  if (u_mode == 1) {\\n"\n'
    '  "    gl_FragColor = vec4(\\n"\n'
    '  "      clamp( 0.437*L.r + 0.449*L.g + 0.164*L.b - 0.011*R.r - 0.032*R.g - 0.007*R.b, 0.0, 1.0),\\n"\n'
    '  "      clamp(-0.062*L.r - 0.062*L.g - 0.024*L.b + 0.377*R.r + 0.761*R.g + 0.009*R.b, 0.0, 1.0),\\n"\n'
    '  "      clamp(-0.048*L.r - 0.050*L.g - 0.017*L.b - 0.026*R.r - 0.093*R.g + 1.234*R.b, 0.0, 1.0),\\n"\n'
    '  "      1.0);\\n"\n'
    '  "  } else if (u_mode == 2) {\\n"\n'
    '  "    gl_FragColor = vec4(L.r * 1.05, R.g * 0.97, R.b * 0.97, 1.0);\\n"\n'
    '  "  } else {\\n"\n'
    '  "    float lumaL = dot(L, vec3(0.299, 0.587, 0.114));\\n"\n'
    '  "    gl_FragColor = vec4(lumaL, clamp(R.g - lumaL*0.12, 0.0, 1.0), clamp(R.b - lumaL*0.12, 0.0, 1.0), 1.0);\\n"\n'
    '  "  }\\n"\n'
    '  "}\\n";',
    "hw includes")

src = replace_once(src,
    "glBindTexture(GL_TEXTURE_EXTERNAL_OES, plane.id);",
    "glBindTexture(GL_TEXTURE_EXTERNAL_OES, plane.id);\n"
    "  static int s_drmFd = -1;\n"
    "  static int s_dumbFd = -1;\n"
    "  static void* s_dumbMap = MAP_FAILED;\n"
    "  static uint32_t s_dumbPitch = 0;\n"
    "  static uint64_t s_dumbSize = 0;\n"
    "  static uint32_t s_dumbHandle = 0;\n"
    "  static int s_dumbW = 0, s_dumbH = 0;\n"
    "  static float s_shiftVal = 0.0f;\n"
    "  static int s_modeVal = 0, s_isActive = 0, s_computeOnly = 0, s_skipFrames = 0, s_lowresFx = 0, s_depTest = 0;\n"
    "  static int s_frameCounter2 = 0;\n"
    "  static GLuint s_fxFboId = 0, s_fxFboTex = 0, s_fxProg = 0;\n"
    "  static GLint s_fxPosLoc = -1, s_fxSrcLoc = -1, s_fxDepthLoc = -1, s_fxShiftLoc = -1, s_fxModeLoc = -1;\n"
    "  static int s_fxW = 0, s_fxH = 0;\n"
    "  static GLuint s_yTex = 0, s_uvTex = 0;\n"
    "  static unsigned char* s_packedBuf = nullptr;\n"
    "  static GLuint s_packedTex = 0;\n"
    "  static int s_logTimer = 0;\n"
    "  static double s_totalMinMs = 1e9, s_totalMaxMs = 0, s_totalSumMs = 0;\n"
    "  static int s_totalCount = 0;\n"
    "  auto t_total0 = std::chrono::steady_clock::now();\n"
    "  if (s_logTimer++ % 30 == 0) {\n"
    "    if (access(\"/storage/.config/stereo3d_compute_only\", F_OK) == 0) { s_isActive = 1; s_modeVal = 0; s_computeOnly = 1; }\n"
    "    else if (access(\"/storage/.config/stereo3d_color\", F_OK) == 0) { s_isActive = 1; s_modeVal = 2; s_computeOnly = 0; }\n"
    "    else if (access(\"/storage/.config/stereo3d_dubois\", F_OK) == 0) { s_isActive = 1; s_modeVal = 1; s_computeOnly = 0; }\n"
    "    else if (access(\"/storage/.config/stereo3d\", F_OK) == 0) { s_isActive = 1; s_modeVal = 0; s_computeOnly = 0; }\n"
    "    else { s_isActive = 0; s_computeOnly = 0; }\n"
    "    s_skipFrames = (access(\"/storage/.config/stereo3d_skip_frames\", F_OK) == 0) ? 1 : 0;\n"
    "    s_lowresFx = (access(\"/storage/.config/stereo3d_lowres_fx\", F_OK) == 0) ? 1 : 0;\n"
    "    s_depTest = (access(\"/storage/.config/stereo3d_nv12_packed\", F_OK) == 0) ? 7\n"
    "                : (access(\"/storage/.config/stereo3d_nv12_trivial\", F_OK) == 0) ? 6\n"
    "                : (access(\"/storage/.config/stereo3d_nv12_shader\", F_OK) == 0) ? 5\n"
    "                : (access(\"/storage/.config/stereo3d_dep_test3\", F_OK) == 0) ? 3\n"
    "                : (access(\"/storage/.config/stereo3d_dep_test2\", F_OK) == 0) ? 2\n"
    "                : (access(\"/storage/.config/stereo3d_dep_test\", F_OK) == 0) ? 1 : 0;\n"
    "  }\n"
    "  s_shiftVal = s_shiftVal * 0.85f + (s_isActive ? MAX_SHIFT : 0.0f) * 0.15f;\n"
    "\n"
    "  if (s_shiftVal > 0.001f) {\n"
    "    CVideoBufferDRMPRIME* drmBuffer = dynamic_cast<CVideoBufferDRMPRIME*>(m_buffers[index].videoBuffer);\n"
    "    if (drmBuffer) {\n"
    "      AVFrame* frame = drmBuffer->GetFrame();\n"
    "      if (frame && frame->format == AV_PIX_FMT_DRM_PRIME) {\n"
    "        AVDRMFrameDescriptor* desc = (AVDRMFrameDescriptor*)frame->data[0];\n"
    "        if (desc && desc->nb_objects > 0) {\n"
    "          int fd = desc->objects[0].fd;\n"
    "          uint32_t pitch = desc->layers[0].planes[0].pitch;\n"
    "          int dw = 320; int dh = 180;\n"
    "\n"
    "          if (s_dumbW != dw || s_dumbH != dh) {\n"
    "            if (s_dumbMap != MAP_FAILED) { munmap(s_dumbMap, s_dumbSize); s_dumbMap = MAP_FAILED; }\n"
    "            if (s_dumbFd >= 0) { close(s_dumbFd); s_dumbFd = -1; }\n"
    "            if (s_dumbHandle != 0 && s_drmFd >= 0) {\n"
    "              struct stereo3d_drm_mode_destroy_dumb dest = { s_dumbHandle };\n"
    "              ioctl(s_drmFd, STEREO3D_DRM_IOCTL_MODE_DESTROY_DUMB, &dest);\n"
    "              s_dumbHandle = 0;\n"
    "            }\n"
    "            if (s_drmFd < 0) s_drmFd = open(\"/dev/dri/card0\", O_RDWR | O_CLOEXEC);\n"
    "            if (s_drmFd >= 0) {\n"
    "              struct stereo3d_drm_mode_create_dumb cr = {0};\n"
    "              cr.width = dw; cr.height = dh * 3 / 2; cr.bpp = 8;\n"
    "              if (ioctl(s_drmFd, STEREO3D_DRM_IOCTL_MODE_CREATE_DUMB, &cr) == 0) {\n"
    "                s_dumbHandle = cr.handle; s_dumbPitch = cr.pitch; s_dumbSize = cr.size;\n"
    "                struct stereo3d_drm_prime_handle pr = {0};\n"
    "                pr.handle = cr.handle; pr.flags = O_CLOEXEC | O_RDWR;\n"
    "                if (ioctl(s_drmFd, STEREO3D_DRM_IOCTL_PRIME_HANDLE_TO_FD, &pr) == 0) {\n"
    "                  s_dumbFd = pr.fd;\n"
    "                  struct stereo3d_drm_mode_map_dumb mapArg = {0};\n"
    "                  mapArg.handle = cr.handle;\n"
    "                  if (ioctl(s_drmFd, STEREO3D_DRM_IOCTL_MODE_MAP_DUMB, &mapArg) == 0) {\n"
    "                    s_dumbMap = mmap(0, s_dumbSize, PROT_READ | PROT_WRITE, MAP_SHARED, s_drmFd, mapArg.offset);\n"
    "                    if (s_dumbMap != MAP_FAILED) {\n"
    "                      s_dumbW = dw; s_dumbH = dh;\n"
    "                      if (m_depthW != dw || m_depthH != dh || !m_depthMap) {\n"
    "                        if (m_depthMap) free(m_depthMap);\n"
    "                        if (m_depthAccum) free(m_depthAccum);\n"
    "                        if (m_depthPrev) free(m_depthPrev);\n"
    "                        m_depthMap = (unsigned char*)calloc(dw * dh, 1);\n"
    "                        m_depthAccum = (unsigned char*)calloc(dw * dh, 1);\n"
    "                        m_depthPrev = (unsigned char*)calloc(dw * dh, 1);\n"
    "                        m_depthW = dw; m_depthH = dh;\n"
    "                        if (!m_anaDepthTex) glGenTextures(1, &m_anaDepthTex);\n"
    "                        glBindTexture(GL_TEXTURE_2D, m_anaDepthTex);\n"
    "                        glTexImage2D(GL_TEXTURE_2D, 0, GL_LUMINANCE, dw, dh, 0, GL_LUMINANCE, GL_UNSIGNED_BYTE, NULL);\n"
    "                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);\n"
    "                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);\n"
    "                        if (!s_yTex) glGenTextures(1, &s_yTex);\n"
    "                        glBindTexture(GL_TEXTURE_2D, s_yTex);\n"
    "                        glTexImage2D(GL_TEXTURE_2D, 0, GL_LUMINANCE, dw, dh, 0, GL_LUMINANCE, GL_UNSIGNED_BYTE, NULL);\n"
    "                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);\n"
    "                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);\n"
    "                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);\n"
    "                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);\n"
    "                        if (!s_uvTex) glGenTextures(1, &s_uvTex);\n"
    "                        glBindTexture(GL_TEXTURE_2D, s_uvTex);\n"
    "                        glTexImage2D(GL_TEXTURE_2D, 0, GL_LUMINANCE_ALPHA, dw / 2, dh / 2, 0, GL_LUMINANCE_ALPHA, GL_UNSIGNED_BYTE, NULL);\n"
    "                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);\n"
    "                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);\n"
    "                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);\n"
    "                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);\n"
    "                        if (!s_packedTex) glGenTextures(1, &s_packedTex);\n"
    "                        glBindTexture(GL_TEXTURE_2D, s_packedTex);\n"
    "                        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, dw, dh, 0, GL_RGB, GL_UNSIGNED_BYTE, NULL);\n"
    "                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);\n"
    "                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);\n"
    "                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);\n"
    "                        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);\n"
    "                        if (!s_packedBuf) s_packedBuf = (unsigned char*)malloc((size_t)dw * dh * 3);\n"
    "                      }\n"
    "                      CLog::Log(LOGINFO, \"[Stereo3D] Dumb buffer ready: %dx%d pitch=%d fd=%d (source %dx%d)\",\n"
    "                                dw, dh, s_dumbPitch, s_dumbFd, (int)frame->width, (int)frame->height);\n"
    "                    } else {\n"
    "                      CLog::Log(LOGERROR, \"[Stereo3D] Dumb buffer mmap failed, errno=%d\", errno);\n"
    "                    }\n"
    "                  } else {\n"
    "                    CLog::Log(LOGERROR, \"[Stereo3D] MODE_MAP_DUMB failed, errno=%d\", errno);\n"
    "                  }\n"
    "                } else {\n"
    "                  CLog::Log(LOGERROR, \"[Stereo3D] PRIME_HANDLE_TO_FD failed, errno=%d\", errno);\n"
    "                }\n"
    "              } else {\n"
    "                CLog::Log(LOGERROR, \"[Stereo3D] MODE_CREATE_DUMB failed, errno=%d\", errno);\n"
    "              }\n"
    "            } else {\n"
    "              CLog::Log(LOGERROR, \"[Stereo3D] Failed to open /dev/dri/card0, errno=%d\", errno);\n"
    "            }\n"
    "          }\n"
    "\n"
    "          bool doProcessNow = (!s_skipFrames) || ((s_frameCounter2++ % 2) == 0);\n"
    "          bool rgaOk = false;\n"
    "          auto t0 = std::chrono::steady_clock::now();\n"
    "          if (doProcessNow && s_dumbFd >= 0 && s_dumbMap != MAP_FAILED) {\n"
    "            struct S3D_Rga2Req* req = (struct S3D_Rga2Req*)calloc(1, 1024);\n"
    "            req->render_mode = 0;\n"
    "            req->src.format = 0x12;\n"
    "            req->src.yrgb_addr = fd; req->src.uv_addr = 0; req->src.v_addr = 0;\n"
    "            req->src.vir_w = pitch; req->src.vir_h = frame->height;\n"
    "            req->src.act_w = frame->width; req->src.act_h = frame->height;\n"
    "            req->dst.format = 0x12;\n"
    "            req->dst.yrgb_addr = s_dumbFd; req->dst.uv_addr = 0; req->dst.v_addr = 0;\n"
    "            req->dst.vir_w = s_dumbPitch; req->dst.vir_h = dh;\n"
    "            req->dst.act_w = dw; req->dst.act_h = dh;\n"
    "            req->mmu_info.src0_mmu_flag = 1 | 0x80;\n"
    "            req->mmu_info.dst_mmu_flag  = 1 | 0x80;\n"
    "            int rgaFd = open(\"/dev/rga2\", O_RDWR | O_CLOEXEC);\n"
    "            if (rgaFd < 0) rgaFd = open(\"/dev/rga\", O_RDWR | O_CLOEXEC);\n"
    "            if (rgaFd >= 0) {\n"
    "              if (ioctl(rgaFd, STEREO3D_RGA2_BLIT_SYNC, req) == 0) rgaOk = true;\n"
    "              close(rgaFd);\n"
    "            }\n"
    "            free(req);\n"
    "          }\n"
    "          auto t1 = std::chrono::steady_clock::now();\n"
    "\n"
    "          if (rgaOk) {\n"
    "            struct dma_buf_sync sync = {0};\n"
    "            sync.flags = DMA_BUF_SYNC_START | DMA_BUF_SYNC_READ;\n"
    "            ioctl(s_dumbFd, DMA_BUF_IOCTL_SYNC, &sync);\n"
    "            unsigned char* luma = (unsigned char*)s_dumbMap;\n"
    "            if ((s_depTest == 5 || s_depTest == 6) && s_yTex && s_uvTex) {\n"
    "              glBindTexture(GL_TEXTURE_2D, s_yTex);\n"
    "              if (s_dumbPitch == (uint32_t)dw) {\n"
    "                glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, dw, dh, GL_LUMINANCE, GL_UNSIGNED_BYTE, luma);\n"
    "              } else {\n"
    "                for (int ry = 0; ry < dh; ry++) {\n"
    "                  glTexSubImage2D(GL_TEXTURE_2D, 0, 0, ry, dw, 1, GL_LUMINANCE, GL_UNSIGNED_BYTE, luma + (size_t)ry * s_dumbPitch);\n"
    "                }\n"
    "              }\n"
    "              unsigned char* uvPlane = luma + (size_t)s_dumbPitch * dh;\n"
    "              glBindTexture(GL_TEXTURE_2D, s_uvTex);\n"
    "              if (s_dumbPitch == (uint32_t)dw) {\n"
    "                glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, dw / 2, dh / 2, GL_LUMINANCE_ALPHA, GL_UNSIGNED_BYTE, uvPlane);\n"
    "              } else {\n"
    "                for (int ry = 0; ry < dh / 2; ry++) {\n"
    "                  glTexSubImage2D(GL_TEXTURE_2D, 0, 0, ry, dw / 2, 1, GL_LUMINANCE_ALPHA, GL_UNSIGNED_BYTE, uvPlane + (size_t)ry * s_dumbPitch);\n"
    "                }\n"
    "              }\n"
    "              glBindTexture(GL_TEXTURE_2D, 0);\n"
    "            }\n"
    "            if (s_depTest == 7 && s_packedTex && s_packedBuf) {\n"
    "              unsigned char* uvPlaneP = luma + (size_t)s_dumbPitch * dh;\n"
    "              for (int py = 0; py < dh; py++) {\n"
    "                unsigned char* yRow = luma + (size_t)py * s_dumbPitch;\n"
    "                unsigned char* uvRow = uvPlaneP + (size_t)(py / 2) * s_dumbPitch;\n"
    "                unsigned char* outRow = s_packedBuf + (size_t)py * dw * 3;\n"
    "                for (int px = 0; px < dw; px++) {\n"
    "                  int uvCol = (px / 2) * 2;\n"
    "                  outRow[px * 3 + 0] = yRow[px];\n"
    "                  outRow[px * 3 + 1] = uvRow[uvCol];\n"
    "                  outRow[px * 3 + 2] = uvRow[uvCol + 1];\n"
    "                }\n"
    "              }\n"
    "              glBindTexture(GL_TEXTURE_2D, s_packedTex);\n"
    "              glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, dw, dh, GL_RGB, GL_UNSIGNED_BYTE, s_packedBuf);\n"
    "              glBindTexture(GL_TEXTURE_2D, 0);\n"
    "            }\n"
    "\n"
    "            uint8x16_t v_black = vdupq_n_u8(15); uint8x16_t v_white = vdupq_n_u8(235);\n"
    "            for (int y = 0; y < dh; y++) {\n"
    "              uint8_t gv = (uint8_t)((y * 180) / dh);\n"
    "              uint8x16_t v_grad = vdupq_n_u8(gv);\n"
    "              unsigned char *row = luma + y * s_dumbPitch;\n"
    "              unsigned char *prev = m_depthPrev + y * dw;\n"
    "              unsigned char *out = m_depthMap + y * dw;\n"
    "              unsigned char *acc = m_depthAccum + y * dw;\n"
    "              int x = 0;\n"
    "              for (; x <= dw - 16; x += 16) {\n"
    "                uint8x16_t c = vld1q_u8(row + x);\n"
    "                uint8x16_t p = vld1q_u8(prev + x);\n"
    "                uint8x16_t motion = vabdq_u8(c, p);\n"
    "                uint8x16_t is_text = vorrq_u8(vcleq_u8(c, v_black), vcgeq_u8(c, v_white));\n"
    "                motion = vandq_u8(motion, vmvnq_u8(is_text));\n"
    "                uint8x16_t raw = vqaddq_u8(v_grad, vshrq_n_u8(motion, 2));\n"
    "                uint8x16_t old_a = vld1q_u8(acc + x);\n"
    "                uint8x16_t smooth = vrhaddq_u8(vrhaddq_u8(old_a, old_a), raw);\n"
    "                vst1q_u8(acc + x, smooth); vst1q_u8(out + x, smooth); vst1q_u8(prev + x, c);\n"
    "              }\n"
    "              for (; x < dw; x++) {\n"
    "                int cv = row[x], pv = prev[x]; int m = abs(cv - pv); if (cv < 15 || cv > 235) m = 0;\n"
    "                int raw = gv + (m >> 2); if (raw > 255) raw = 255;\n"
    "                int sm = (acc[x] * 3 + raw) >> 2; acc[x] = out[x] = (unsigned char)sm; prev[x] = cv;\n"
    "              }\n"
    "            }\n"
    "            auto t2 = std::chrono::steady_clock::now();\n"
    "\n"
    "            for (int y = 0; y < dh; y++) {\n"
    "              unsigned char *row = m_depthMap + y * dw;\n"
    "              unsigned char *out = m_depthAccum + y * dw;\n"
    "              int x = 0;\n"
    "              for (; x <= dw - 16; x += 16) {\n"
    "                uint8x16_t l = vld1q_u8(row + (x > 0 ? x - 1 : 0));\n"
    "                uint8x16_t c = vld1q_u8(row + x);\n"
    "                uint8x16_t r = vld1q_u8(row + (x + 1 < dw ? x + 1 : dw - 1));\n"
    "                uint8x16_t left = vextq_u8(l, c, 15); uint8x16_t right = vextq_u8(c, r, 1);\n"
    "                uint8x16_t blur = vrhaddq_u8(vrhaddq_u8(left, c), right);\n"
    "                vst1q_u8(out + x, blur);\n"
    "              }\n"
    "              for (; x < dw; x++) {\n"
    "                int xl = x > 0 ? x - 1 : 0; int xr = x < dw - 1 ? x + 1 : dw - 1;\n"
    "                out[x] = (unsigned char)((row[xl] + row[x] * 2 + row[xr]) >> 2);\n"
    "              }\n"
    "            }\n"
    "            memcpy(m_depthMap, m_depthAccum, (size_t)dw * dh);\n"
    "            auto t3 = std::chrono::steady_clock::now();\n"
    "\n"
    "            glBindTexture(GL_TEXTURE_2D, m_anaDepthTex);\n"
    "            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, dw, dh, GL_LUMINANCE, GL_UNSIGNED_BYTE, m_depthMap);\n"
    "            glBindTexture(GL_TEXTURE_2D, 0);\n"
    "            sync.flags = DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ;\n"
    "            ioctl(s_dumbFd, DMA_BUF_IOCTL_SYNC, &sync);\n"
    "\n"
    "            if (s_logTimer % 60 == 0) {\n"
    "              double msRga = std::chrono::duration<double, std::milli>(t1 - t0).count();\n"
    "              double msNeon = std::chrono::duration<double, std::milli>(t2 - t1).count();\n"
    "              double msBlur = std::chrono::duration<double, std::milli>(t3 - t2).count();\n"
    "              CLog::Log(LOGDEBUG, \"[Stereo3D-TIMING] RGA=%.2fms NEON=%.2fms Blur=%.2fms depthSize=%dx%d (rgaOk=%d)\",\n"
    "                        msRga, msNeon, msBlur, dw, dh, (int)rgaOk);\n"
    "            }\n"
    "          }\n"
    "          if (s_lowresFx && m_anaDepthTex && frame->width > 0 && frame->height > 0) {\n"
    "            int fxW = frame->width / 2; int fxH = frame->height / 2;\n"
    "            if (s_fxW != fxW || s_fxH != fxH || !s_fxFboId) {\n"
    "              if (s_fxFboId) glDeleteFramebuffers(1, &s_fxFboId);\n"
    "              if (s_fxFboTex) glDeleteTextures(1, &s_fxFboTex);\n"
    "              glGenTextures(1, &s_fxFboTex);\n"
    "              glBindTexture(GL_TEXTURE_2D, s_fxFboTex);\n"
    "              glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, fxW, fxH, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);\n"
    "              glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);\n"
    "              glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);\n"
    "              glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);\n"
    "              glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);\n"
    "              glGenFramebuffers(1, &s_fxFboId);\n"
    "              glBindFramebuffer(GL_FRAMEBUFFER, s_fxFboId);\n"
    "              glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, s_fxFboTex, 0);\n"
    "              if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {\n"
    "                CLog::Log(LOGERROR, \"[Stereo3D-FX] FBO incomplete\");\n"
    "              }\n"
    "              glBindFramebuffer(GL_FRAMEBUFFER, 0);\n"
    "              s_fxW = fxW; s_fxH = fxH;\n"
    "            }\n"
    "            if (!s_fxProg) {\n"
    "              s_fxProg = Stereo3D_LinkProgram(Stereo3D_FxVS, Stereo3D_FxFS);\n"
    "              if (s_fxProg) {\n"
    "                s_fxPosLoc = glGetAttribLocation(s_fxProg, \"a_pos\");\n"
    "                s_fxSrcLoc = glGetUniformLocation(s_fxProg, \"u_srcTex\");\n"
    "                s_fxDepthLoc = glGetUniformLocation(s_fxProg, \"u_depthTex\");\n"
    "                s_fxShiftLoc = glGetUniformLocation(s_fxProg, \"u_shift\");\n"
    "                s_fxModeLoc = glGetUniformLocation(s_fxProg, \"u_mode\");\n"
    "              }\n"
    "            }\n"
    "            if (s_fxFboId && s_fxProg) {\n"
    "              GLint prevFbo = 0, prevProg = 0, prevVP[4] = {0,0,0,0};\n"
    "              glGetIntegerv(GL_FRAMEBUFFER_BINDING, &prevFbo);\n"
    "              glGetIntegerv(GL_CURRENT_PROGRAM, &prevProg);\n"
    "              glGetIntegerv(GL_VIEWPORT, prevVP);\n"
    "              glBindFramebuffer(GL_FRAMEBUFFER, s_fxFboId);\n"
    "              glViewport(0, 0, s_fxW, s_fxH);\n"
    "              glUseProgram(s_fxProg);\n"
    "              static const float quad[8] = {-1,-1, 1,-1, -1,1, 1,1};\n"
    "              if (s_fxPosLoc >= 0) {\n"
    "                glEnableVertexAttribArray(s_fxPosLoc);\n"
    "                glVertexAttribPointer(s_fxPosLoc, 2, GL_FLOAT, GL_FALSE, 0, quad);\n"
    "              }\n"
    "              glActiveTexture(GL_TEXTURE0);\n"
    "              glBindTexture(GL_TEXTURE_EXTERNAL_OES, plane.id);\n"
    "              if (s_fxSrcLoc != -1) glUniform1i(s_fxSrcLoc, 0);\n"
    "              glActiveTexture(GL_TEXTURE1);\n"
    "              glBindTexture(GL_TEXTURE_2D, m_anaDepthTex);\n"
    "              if (s_fxDepthLoc != -1) glUniform1i(s_fxDepthLoc, 1);\n"
    "              if (s_fxShiftLoc != -1) glUniform1f(s_fxShiftLoc, s_shiftVal);\n"
    "              if (s_fxModeLoc != -1) glUniform1i(s_fxModeLoc, s_modeVal);\n"
    "              glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);\n"
    "              if (s_fxPosLoc >= 0) glDisableVertexAttribArray(s_fxPosLoc);\n"
    "              glBindFramebuffer(GL_FRAMEBUFFER, prevFbo);\n"
    "              glViewport(prevVP[0], prevVP[1], prevVP[2], prevVP[3]);\n"
    "              glUseProgram(prevProg);\n"
    "              glActiveTexture(GL_TEXTURE0);\n"
    "              glBindTexture(GL_TEXTURE_EXTERNAL_OES, plane.id);\n"
    "            }\n"
    "          }\n"
    "          auto t_total1 = std::chrono::steady_clock::now();\n"
    "          double msTotal = std::chrono::duration<double, std::milli>(t_total1 - t_total0).count();\n"
    "          if (msTotal < s_totalMinMs) s_totalMinMs = msTotal;\n"
    "          if (msTotal > s_totalMaxMs) s_totalMaxMs = msTotal;\n"
    "          s_totalSumMs += msTotal;\n"
    "          s_totalCount++;\n"
    "          if (s_totalCount >= 60) {\n"
    "            CLog::Log(LOGDEBUG, \"[Stereo3D-TOTAL] min=%.2fms max=%.2fms avg=%.2fms over %d frames (full per-frame patch cost, entry-to-exit)\",\n"
    "                      s_totalMinMs, s_totalMaxMs, s_totalSumMs / s_totalCount, s_totalCount);\n"
    "            s_totalMinMs = 1e9; s_totalMaxMs = 0; s_totalSumMs = 0; s_totalCount = 0;\n"
    "          }\n"
    "        }\n"
    "      }\n"
    "    }\n"
    "  }",
    "hw DMA-buf RGA NEON v4")

src = replace_once(src,
    "renderSystem->EnableGUIShader(SM_TEXTURE_RGBA_OES);",
    "renderSystem->EnableGUIShader(SM_TEXTURE_RGBA_OES);\n"
    "  {\n"
    "    static GLint s_hShift = -2, s_hMode = -2, s_hActive = -2, s_hDepth = -2;\n"
    "    static GLint s_hUseFx = -2, s_hFxTex = -2, s_hDepTest = -2, s_hYTex = -2, s_hUvTex = -2, s_hPackedTex = -2;\n"
    "    static GLint s_cachedProg = 0;\n"
    "    static int s_progRecheckCounter = 0;\n"
    "    if (s_cachedProg == 0 || (s_progRecheckCounter++ % 300 == 0)) {\n"
    "      glGetIntegerv(GL_CURRENT_PROGRAM, &s_cachedProg);\n"
    "    }\n"
    "    GLint prog = s_cachedProg;\n"
    "    if (prog > 0 && s_shiftVal > 0.001f) {\n"
    "      if (s_hShift == -2) {\n"
    "        s_hShift = glGetUniformLocation(prog, \"u_anaShift\");\n"
    "        s_hMode = glGetUniformLocation(prog, \"u_stereoMode\");\n"
    "        s_hActive = glGetUniformLocation(prog, \"u_stereo3d\");\n"
    "        s_hDepth = glGetUniformLocation(prog, \"m_sampDepth\");\n"
    "        s_hUseFx = glGetUniformLocation(prog, \"u_useLowresFx\");\n"
    "        s_hFxTex = glGetUniformLocation(prog, \"u_fxTex\");\n"
    "        s_hDepTest = glGetUniformLocation(prog, \"u_depTest\");\n"
    "        s_hYTex = glGetUniformLocation(prog, \"u_yTex\");\n"
    "        s_hUvTex = glGetUniformLocation(prog, \"u_uvTex\");\n"
    "        s_hPackedTex = glGetUniformLocation(prog, \"u_packedTex\");\n"
    "      }\n"
    "      if (s_hShift != -1) {\n"
    "        float effectiveShift = s_computeOnly ? 0.0f : s_shiftVal;\n"
    "        int effectiveActive = s_computeOnly ? 0 : 1;\n"
    "        glUniform1f(s_hShift, effectiveShift);\n"
    "        if (s_hMode != -1) glUniform1i(s_hMode, s_modeVal);\n"
    "        if (s_hActive != -1) glUniform1i(s_hActive, effectiveActive);\n"
    "        if (m_anaDepthTex && s_hDepth != -1 && !s_computeOnly) {\n"
    "          glActiveTexture(GL_TEXTURE1);\n"
    "          glBindTexture(GL_TEXTURE_2D, m_anaDepthTex);\n"
    "          glUniform1i(s_hDepth, 1);\n"
    "          glActiveTexture(GL_TEXTURE0);\n"
    "        }\n"
    "        if (s_hUseFx != -1) glUniform1i(s_hUseFx, (s_lowresFx && s_fxFboTex && !s_computeOnly) ? 1 : 0);\n"
    "        if (s_hDepTest != -1) glUniform1i(s_hDepTest, (!s_computeOnly && !s_lowresFx) ? s_depTest : 0);\n"
    "        if ((s_depTest == 5 || s_depTest == 6) && s_yTex && s_uvTex) {\n"
    "          if (s_hYTex != -1) {\n"
    "            glActiveTexture(GL_TEXTURE3);\n"
    "            glBindTexture(GL_TEXTURE_2D, s_yTex);\n"
    "            glUniform1i(s_hYTex, 3);\n"
    "          }\n"
    "          if (s_hUvTex != -1) {\n"
    "            glActiveTexture(GL_TEXTURE4);\n"
    "            glBindTexture(GL_TEXTURE_2D, s_uvTex);\n"
    "            glUniform1i(s_hUvTex, 4);\n"
    "          }\n"
    "          glActiveTexture(GL_TEXTURE0);\n"
    "        }\n"
    "        if (s_depTest == 7 && s_packedTex && s_hPackedTex != -1) {\n"
    "          glActiveTexture(GL_TEXTURE5);\n"
    "          glBindTexture(GL_TEXTURE_2D, s_packedTex);\n"
    "          glUniform1i(s_hPackedTex, 5);\n"
    "          glActiveTexture(GL_TEXTURE0);\n"
    "        }\n"
    "        static bool s_fxDiagLogged = false;\n"
    "        if (!s_fxDiagLogged && s_lowresFx) {\n"
    "          s_fxDiagLogged = true;\n"
    "          CLog::Log(LOGINFO, \"[Stereo3D-FX] s_hUseFx=%d s_hFxTex=%d fboTex=%u fboId=%u willEnable=%d\",\n"
    "                    s_hUseFx, s_hFxTex, s_fxFboTex, s_fxFboId,\n"
    "                    (s_lowresFx && s_fxFboTex && !s_computeOnly) ? 1 : 0);\n"
    "        }\n"
    "        if (s_lowresFx && s_fxFboTex && !s_computeOnly && s_hFxTex != -1) {\n"
    "          glActiveTexture(GL_TEXTURE2);\n"
    "          glBindTexture(GL_TEXTURE_2D, s_fxFboTex);\n"
    "          glUniform1i(s_hFxTex, 2);\n"
    "          glActiveTexture(GL_TEXTURE0);\n"
    "        }\n"
    "      }\n"
    "    }\n"
    "  }",
    "hw stereo uniforms v4.02")
patches.append(make_diff(REL, orig, src))

# 8. gles_shader_rgba_oes.frag
REL  = "system/shaders/GLES/2.0/gles_shader_rgba_oes.frag"
orig = read_file(REL)
src  = orig
src = replace_once(src, "void main ()\n{",
    "uniform float u_anaShift;\n"
    "uniform int   u_stereo3d;\n"
    "uniform int   u_stereoMode;\n"
    "uniform sampler2D m_sampDepth;\n"
    "uniform int u_useLowresFx;\n"
    "uniform sampler2D u_fxTex;\n"
    "uniform int u_depTest;\n"
    "uniform sampler2D u_yTex;\n"
    "uniform sampler2D u_uvTex;\n"
    "uniform sampler2D u_packedTex;\n\n"
    "void main ()\n{",
    "OES uniforms")
src = replace_once(src, "  gl_FragColor = rgb;\n}",
    "  if (u_stereo3d == 1) {\n"
    "    if (u_useLowresFx == 1) {\n"
    "      gl_FragColor = texture2D(u_fxTex, m_cord0.xy);\n"
    "    } else if (u_depTest == 7) {\n"
    "      float d7 = texture2D(m_sampDepth, m_cord0.xy).r;\n"
    "      float s7 = d7 * u_anaShift;\n"
    "      vec2 sampleCoord7 = clamp(m_cord0.xy + vec2(s7, 0.0), 0.001, 0.999);\n"
    "      vec3 yuv7 = texture2D(u_packedTex, sampleCoord7).rgb;\n"
    "      float u7 = yuv7.g - 0.5;\n"
    "      float v7 = yuv7.b - 0.5;\n"
    "      vec3 L7 = rgb.rgb;\n"
    "      vec3 R7 = clamp(vec3(yuv7.r + 1.402*v7, yuv7.r - 0.344*u7 - 0.714*v7, yuv7.r + 1.772*u7) * m_contrast + vec3(m_brightness), 0.0, 1.0);\n"
    "      if (u_stereoMode == 1) {\n"
    "        gl_FragColor = vec4(\n"
    "          clamp( 0.437*L7.r + 0.449*L7.g + 0.164*L7.b - 0.011*R7.r - 0.032*R7.g - 0.007*R7.b, 0.0, 1.0),\n"
    "          clamp(-0.062*L7.r - 0.062*L7.g - 0.024*L7.b + 0.377*R7.r + 0.761*R7.g + 0.009*R7.b, 0.0, 1.0),\n"
    "          clamp(-0.048*L7.r - 0.050*L7.g - 0.017*L7.b - 0.026*R7.r - 0.093*R7.g + 1.234*R7.b, 0.0, 1.0),\n"
    "          1.0);\n"
    "      } else if (u_stereoMode == 2) {\n"
    "        gl_FragColor = vec4(L7.r * 1.05, R7.g * 0.97, R7.b * 0.97, 1.0);\n"
    "      } else {\n"
    "        float lumaL7 = dot(L7, vec3(0.299, 0.587, 0.114));\n"
    "        gl_FragColor = vec4(lumaL7, clamp(R7.g - lumaL7 * 0.12, 0.0, 1.0), clamp(R7.b - lumaL7 * 0.12, 0.0, 1.0), 1.0);\n"
    "      }\n"
    "    } else if (u_depTest == 6) {\n"
    "      vec3 L6 = rgb.rgb;\n"
    "      float y6 = texture2D(u_yTex, m_cord0.xy).r;\n"
    "      vec4 uv6 = texture2D(u_uvTex, m_cord0.xy);\n"
    "      gl_FragColor = vec4((L6.r + y6) * 0.5, (L6.g + uv6.r) * 0.5, (L6.b + uv6.a) * 0.5, 1.0);\n"
    "    } else if (u_depTest == 5) {\n"
    "      float d5 = texture2D(m_sampDepth, m_cord0.xy).r;\n"
    "      float s5 = d5 * u_anaShift;\n"
    "      vec3 L5 = rgb.rgb;\n"
    "      vec4 rS4 = texture2D(u_yTex, clamp(m_cord0.xy + vec2(s5, 0.0), 0.001, 0.999));\n"
    "      gl_FragColor = vec4(L5.r, rS4.r, rS4.r, 1.0);\n"
    "    } else if (u_depTest == 3) {\n"
    "      vec3 L3 = rgb.rgb;\n"
    "      vec3 R3 = texture2D(m_sampDepth, m_cord0.xy).rgb;\n"
    "      gl_FragColor = vec4((L3 + R3) * 0.5, 1.0);\n"
    "    } else if (u_depTest == 2) {\n"
    "      vec3 L2 = rgb.rgb;\n"
    "      vec3 R2 = texture2D(m_samp0, clamp(m_cord0.xy + vec2(0.01, 0.0), 0.001, 0.999)).rgb;\n"
    "      gl_FragColor = vec4((L2 + R2) * 0.5, 1.0);\n"
    "    } else if (u_depTest == 1) {\n"
    "      float dIgnored = texture2D(m_sampDepth, m_cord0.xy).r;\n"
    "      vec3 L = rgb.rgb;\n"
    "      vec4 rS = texture2D(m_samp0, clamp(m_cord0.xy + vec2(0.01, 0.0), 0.001, 0.999));\n"
    "      vec3 R = clamp(rS.rgb * m_contrast + vec3(m_brightness) + dIgnored * 0.0001, 0.0, 1.0);\n"
    "      if (u_stereoMode == 1) {\n"
    "        gl_FragColor = vec4(\n"
    "          clamp( 0.437*L.r + 0.449*L.g + 0.164*L.b - 0.011*R.r - 0.032*R.g - 0.007*R.b, 0.0, 1.0),\n"
    "          clamp(-0.062*L.r - 0.062*L.g - 0.024*L.b + 0.377*R.r + 0.761*R.g + 0.009*R.b, 0.0, 1.0),\n"
    "          clamp(-0.048*L.r - 0.050*L.g - 0.017*L.b - 0.026*R.r - 0.093*R.g + 1.234*R.b, 0.0, 1.0),\n"
    "          1.0);\n"
    "      } else if (u_stereoMode == 2) {\n"
    "        gl_FragColor = vec4(L.r * 1.05, R.g * 0.97, R.b * 0.97, 1.0);\n"
    "      } else {\n"
    "        float lumaL = dot(L, vec3(0.299, 0.587, 0.114));\n"
    "        gl_FragColor = vec4(lumaL, clamp(R.g - lumaL * 0.12, 0.0, 1.0), clamp(R.b - lumaL * 0.12, 0.0, 1.0), 1.0);\n"
    "      }\n"
    "    } else {\n"
    "    float d = texture2D(m_sampDepth, m_cord0.xy).r;\n"
    "    float s = d * u_anaShift;\n"
    "    vec3 L = rgb.rgb;\n"
    "    vec4 rS = texture2D(m_samp0, clamp(m_cord0.xy + vec2(s, 0.0), 0.001, 0.999));\n"
    "    vec3 R = clamp(rS.rgb * m_contrast + vec3(m_brightness), 0.0, 1.0);\n"
    "    if (u_stereoMode == 1) {\n"
    "      gl_FragColor = vec4(\n"
    "        clamp( 0.437*L.r + 0.449*L.g + 0.164*L.b - 0.011*R.r - 0.032*R.g - 0.007*R.b, 0.0, 1.0),\n"
    "        clamp(-0.062*L.r - 0.062*L.g - 0.024*L.b + 0.377*R.r + 0.761*R.g + 0.009*R.b, 0.0, 1.0),\n"
    "        clamp(-0.048*L.r - 0.050*L.g - 0.017*L.b - 0.026*R.r - 0.093*R.g + 1.234*R.b, 0.0, 1.0),\n"
    "        1.0);\n"
    "    } else if (u_stereoMode == 2) {\n"
    "      gl_FragColor = vec4(L.r * 1.05, R.g * 0.97, R.b * 0.97, 1.0);\n"
    "    } else {\n"
    "      float lumaL = dot(L, vec3(0.299, 0.587, 0.114));\n"
    "      gl_FragColor = vec4(lumaL, clamp(R.g - lumaL * 0.12, 0.0, 1.0), clamp(R.b - lumaL * 0.12, 0.0, 1.0), 1.0);\n"
    "    }\n"
    "    }\n"
    "  } else {\n"
    "    gl_FragColor = rgb;\n"
    "  }\n"
    "}",
    "OES stereo v4.05")
patches.append(make_diff(REL, orig, src))

os.makedirs(OUT_DIR, exist_ok=True)
combined = "".join(p for p in patches if p)
if not combined:
    print("\n[ERROR] No patches generated")
    sys.exit(1)
with open(OUT_FILE, "w", encoding="utf-8") as f:
    f.write(combined)
print(f"\n[SUCCESS] {OUT_FILE}")
print(f"          {len(combined.splitlines())} lines")
