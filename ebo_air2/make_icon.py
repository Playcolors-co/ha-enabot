from PIL import Image, ImageDraw, ImageFont, ImageFilter
import math

S = 4  # supersampling

def lerp(a, b, t): return tuple(int(a[i]+(b[i]-a[i])*t) for i in range(3))

def rounded_rect_gradient(size, radius, top, bot):
    w=h=size
    img=Image.new("RGB",(w,h))
    px=img.load()
    for y in range(h):
        c=lerp(top,bot,y/h)
        for x in range(w): px[x,y]=c
    # rounded-corner mask
    mask=Image.new("L",(w,h),0)
    md=ImageDraw.Draw(mask)
    md.rounded_rectangle([0,0,w-1,h-1],radius=radius,fill=255)
    out=Image.new("RGBA",(w,h),(0,0,0,0))
    out.paste(img,(0,0),mask)
    return out

def draw_ebo(size, with_bg=True):
    W=H=size*S
    if with_bg:
        img=rounded_rect_gradient(W, int(W*0.22), (11,61,71), (16,112,122)).convert("RGBA")
    else:
        img=Image.new("RGBA",(W,H),(0,0,0,0))
    d=ImageDraw.Draw(img)
    cx=W//2; cy=int(H*0.54); r=int(W*0.34)
    # ground shadow
    sh=Image.new("RGBA",(W,H),(0,0,0,0))
    ds=ImageDraw.Draw(sh)
    ds.ellipse([cx-r*0.9, cy+r*0.72, cx+r*0.9, cy+r*1.02], fill=(0,0,0,90))
    sh=sh.filter(ImageFilter.GaussianBlur(W*0.02))
    img=Image.alpha_composite(img, sh)
    d=ImageDraw.Draw(img)
    # sphere body (light radial gradient)
    body=Image.new("RGBA",(W,H),(0,0,0,0))
    bp=body.load()
    for y in range(cy-r, cy+r):
        for x in range(cx-r, cx+r):
            dx=x-cx; dy=y-cy
            dist=math.hypot(dx,dy)
            if dist<=r:
                t=min(1.0,(math.hypot(dx+r*0.35,dy+r*0.35))/(r*1.6))
                col=lerp((244,250,251),(197,220,224),t)
                bp[x,y]=col+(255,)
    img=Image.alpha_composite(img, body)
    d=ImageDraw.Draw(img)
    # dark screen (face) band
    fw=int(r*1.5); fh=int(r*0.78)
    fx0=cx-fw//2; fy0=cy-int(r*0.42); fx1=cx+fw//2; fy1=fy0+fh
    face=Image.new("RGBA",(W,H),(0,0,0,0))
    df=ImageDraw.Draw(face)
    df.rounded_rectangle([fx0,fy0,fx1,fy1], radius=int(fh*0.5), fill=(14,26,31,255))
    # clip the band to the body circle
    cmask=Image.new("L",(W,H),0)
    ImageDraw.Draw(cmask).ellipse([cx-r,cy-r,cx+r,cy+r],fill=255)
    face.putalpha(Image.composite(face.getchannel("A"), Image.new("L",(W,H),0), cmask))
    img=Image.alpha_composite(img, face)
    d=ImageDraw.Draw(img)
    # cyan eyes with glow
    eye_r=int(r*0.16); ey=cy-int(r*0.02); ex=int(r*0.32)
    glow=Image.new("RGBA",(W,H),(0,0,0,0))
    dg=ImageDraw.Draw(glow)
    for sgn in (-1,1):
        dg.ellipse([cx+sgn*ex-eye_r*1.7, ey-eye_r*1.7, cx+sgn*ex+eye_r*1.7, ey+eye_r*1.7], fill=(56,224,224,120))
    glow=glow.filter(ImageFilter.GaussianBlur(W*0.012))
    img=Image.alpha_composite(img, glow)
    d=ImageDraw.Draw(img)
    for sgn in (-1,1):
        d.ellipse([cx+sgn*ex-eye_r, ey-eye_r, cx+sgn*ex+eye_r, ey+eye_r], fill=(64,232,232,255))
        # highlight
        hr=int(eye_r*0.36)
        d.ellipse([cx+sgn*ex-hr-eye_r*0.25, ey-hr-eye_r*0.3, cx+sgn*ex+hr-eye_r*0.25, ey+hr-eye_r*0.3], fill=(230,255,255,255))
    return img

# icon 512
icon=draw_ebo(512, with_bg=True).resize((512,512), Image.LANCZOS)
icon.save("addon/ebo_air2/icon.png")
print("icon.png", icon.size)

# wide logo with text
LW,LH=1000,320
S2=2
logo=Image.new("RGBA",(LW*S2,LH*S2),(0,0,0,0))
robot=draw_ebo(320, with_bg=False).resize((LH*S2-40, LH*S2-40), Image.LANCZOS)
logo.alpha_composite(robot,(20,20))
d=ImageDraw.Draw(logo)
def font(sz):
    for p in ["/System/Library/Fonts/SFNSRounded.ttf","/System/Library/Fonts/HelveticaNeue.ttc","/System/Library/Fonts/Helvetica.ttc","/Library/Fonts/Arial.ttf"]:
        try: return ImageFont.truetype(p, sz)
        except: pass
    return ImageFont.load_default()
tx=(LH-20)*S2
d.text((tx, 90*S2), "EBO Air 2", font=font(150), fill=(238,246,247,255))
d.text((tx+4, 210*S2), "Home Assistant", font=font(64), fill=(64,232,232,255))
logo=logo.resize((LW,LH), Image.LANCZOS)
logo.save("addon/ebo_air2/logo.png")
print("logo.png", logo.size)

# --- logo v2: teal background like the icon, white text ---
LW,LH=1000,340; S2=2
bg=rounded_rect_gradient(LH*S2, int(LH*S2*0.16), (11,61,71),(16,112,122))
logo=Image.new("RGBA",(LW*S2,LH*S2),(0,0,0,0))
# full-width background band
band=rounded_rect_gradient(LW*S2, int(LH*S2*0.16), (11,61,71),(16,112,122))
band=band.resize((LW*S2,LH*S2))
logo.alpha_composite(band,(0,0))
robot=draw_ebo(320, with_bg=False).resize((LH*S2-60, LH*S2-60), Image.LANCZOS)
logo.alpha_composite(robot,(30,30))
d=ImageDraw.Draw(logo)
tx=(LH-10)*S2
d.text((tx, 96*S2), "EBO Air 2", font=font(150), fill=(240,248,249,255))
d.text((tx+4, 214*S2), "per Home Assistant", font=font(62), fill=(72,236,236,255))
logo=logo.resize((LW,LH), Image.LANCZOS)
logo.save("addon/ebo_air2/logo.png")
print("logo.png v2", logo.size)
