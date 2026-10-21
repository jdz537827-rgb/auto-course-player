#!/usr/bin/env python3
"""
qrdecode.py —— 二维码识别（超星扫码签到专用）

把二维码图/截图解成文本，再从文本里抠出超星要的 activeId / enc。

三级后端，能用哪个用哪个，全都装不了也有兜底：
  1. opencv  —— 本机装了 cv2 就用，最快
  2. pyzbar  —— 装了 zbar 就用
  3. 浏览器 + jsQR —— 零依赖兜底：借本机 Edge/Chrome 跑 JS 解码器
     （jsQR 源码已压缩后内嵌在本文件里，不用联网、不用 pip）

单独的用法（排查用，打印识别结果和解析出的参数）：
    python qrdecode.py 二维码.png
    python qrdecode.py 截图.jpg --json

作者: zkk    许可: MIT
"""

import base64
import json
import re
import sys
import urllib.parse
import zlib
from pathlib import Path

# ---------------------------------------------------------------
# jsQR 1.4.0（MIT）源码，zlib 压缩 + base64。运行时解压注入浏览器页面。
# 之所以内嵌而不是从 CDN 拉：目标用户大多在校园网/代理环境，
# 而且二维码里很可能包含签到凭证，不适合往外发请求。
# ---------------------------------------------------------------
JSQR_B64 = (
    "eNrcvX13HMeR5vu39SlaPHdtwALhrvcqUrJvvc5qxpY9kmf37vJoeBpAU4QFArgAKJEz5ne/"
    "WRVPVv+quxqAKHuO784ZQ0QgK18iIzPjiYiMPHj19vL07vzqcvHj+uR6dfr9v12e/7C+uV1d"
    "/OHq7O3Fulm/Or887wsc3Fxd3R0tXq1O765u3h8u/vOTX5y/Orh7f72+erVYv7u+urm7XXzx"
    "xReLX12d/GV9everxS9/udCf3wx1Tf56+MkvfmHk4/FjX/nB4fNPfrG+uF0vNi2c9R1RFb7P"
    "QxP2h+PVm7O+Svvt4MW3m57u1jXX2/5j0V88+cvtv3795NuZDrlCPRvmS3w49A3cri9eLT7t"
    "a397aT06+9Xid0Z+trh7fX7ruqdBHPSsvFnfvb25XPzm18P//WZxMP7VeHTrCi1+8xs/SZXr"
    "w+3dzer6k/GLX7i//vn12rP6dHX6eo2//rC6WZxf3t6tLi7WZza1PcP/88PzsdBuXTfr//ft"
    "+c167CpKjGLz8qX69FKlX75Un78860c2U3tfff16ffq9mxLf3/Nb172dXvcStt3rF772b1m9"
    "K/sLMXH/B17SnvOzD9s9u1mv7taL1eJy/aPv3cHq8mxx/fZuce7+//Luyk2hWHzIr3sme1G/"
    "pxs93ycdP3+28H88mvzh4pmTLyd2U6pG8czN3mQgz/cxu323Pn3rxnS3kY+ZKdVyJL9O3QAO"
    "pqv0SDX4/27oM5JwuLdL3cXqO/Zndbu4uFqdrc92O3R84Rh2d/N2vbeyr23i++r80naLcFM7"
    "i0tIpp2nQHzYbWW6MvpvbslM19qwhywONizQXxwL8PEMh47fuMGp8PPHNrizTuYqPp2RwX0t"
    "aHP9bn13t74ZZWPx6upm8Xp18+bq8r1n7AONnvU7ot+6Rsm4XL1x8mK1by1at8A/navoavr1"
    "4fZa/+PA8WPr+J9urq7XN3fvt1ucfvKL06vLV+ffvb1ZnVysZxfWL9aXb9+s9fde4rb+7Ebw"
    "TMOY/OHD4d4dZe/+6qpxZ+vq7cVdO3R6yvXTqzfXq7vzk/OL87v3ix/P714vLq8un/rJkMA8"
    "MBmXnAz7ZIuN/YalSfdS2B+pWhwvX65vTXAWv5uMd+zqZhD9KbaYrK0XvzqzP/3q2+eLD4tn"
    "e2uwJowLt7v1uI8n3J0VuwMbxdHiV6tfjZL2fGbd25+eP2aGJGLXN1d3V/25fvx6dfvHHy+9"
    "sA2b4wMzcMUZsE3iaHGtCjDSxzS1+/2EM9O+bzpz/fbk4vz0pZOm1y9fPtDda9fdJ0/21fl7"
    "tz8v1pd3N+/HTdudihrB7vagP8zpB3Nt99pIxCn7MO6cP///MJKDF+7fi+VCtF1V62hx74nm"
    "FtAnT9667djpX+end45bnzywFz3ZrKMnbk9a/LC6eKv9ZdHvHP0irM7v/rByFb5zXHDdWvzf"
    "pxer29sFu7cYFMWF+7+RMn51cLa6Wx25feLs7rUv1f9fr2keD1RX7/Df59O/vV6ff/f6zv2x"
    "r+D4Yn35nSv6m9mifQkVtL98GH6OfTg+HRSn9s2127E2Yr84GCo7WlhT7J0kpNezNiPpf/s3"
    "p2Pl9cXqzfX6rLy5Wb23Oha/9pX4oaojz7d6sllHbrlP+vLuaPGeXXAK6MG7xedOHv7618W7"
    "xW+/IMsc6b3/2/vxb7vjwFiGQ2XDuA/bY/3005GXL9678aC1zxbvvn1wPLcz4zla/LAz5/sa"
    "cB//4IBI4FDI8jGNfb3+rm+HTV6sX7lN6O7qWpPgZ3arG/0pdtCLdi8MrvTzgZfuH64f9oGj"
    "fPbZNh/Hz/qV0Df1fNHPT/8v96HJ5eLd7nfjyF2nxZVPP/3h8Pmk0IetidHYNTcjCxySOzh0"
    "n2oFH3NxotAn2kA+HPZqguPpP9aW8k/ry7Wr7p+6P11dvH8ZuL7PbcWhKz3O7ers7I8337w9"
    "ccjy9O6fugO3pZx4RotJq8W/L06e91D07vbCLdNnZ+e3vb701P2yfnZ59fTk/O7HcweUP4zs"
    "26rVdWSLstXfn7QFjl8dXN+cvzm/O//Bcfv2/D8GffOyWt2ud9bGWNA1NP57a7Pra3B/7v+z"
    "9Zfv+gZXDvL3dbsiamWrlBv7n3u2uAL9hmab2Fjz4Vbpi6vvHlfaL4zg+e46O3f05XP3n883"
    "Q3C/7i6VSQdfnPd7wrvpOumbeOc2j3BKts3S74ND12ZWYf+xK/bvW8w+XPxysRnT4ukiOHyM"
    "GN23fB8cft/KfhZ4rr/YZkjPkfO5TXwo+B/rmytN1NYSO5bCe2AGnp1T7PjVzdWbgxfLbw+3"
    "BeDqcv2zqgzGKq2zYy3Yzd+4as6vL6ZnM1e4n+LVYBcbTr0T++eew255z0E35enBlOGrb91e"
    "PiWdfHu4+G/bAjI9D+cGdX7ZGyvX0zHtGdCuENxc/Tgwvb25ubo5eFKvLn/V23dclXeL5ZPD"
    "Rw+Pvd4ZqRvIw+M4eXt+4UD65dWb89XFZDRn6+9u1m43O71av3p1fnruFPDt8VmRXlN5cIhf"
    "Xrqj4vzMKfBqSt86MHnrSq8u9427bwdduFcuxmUyV1G/VlHRreR+V+VTzz7rN4rxa37pFpL7"
    "GJTnc5rl/QuK9R0+OEtuWv+Wkna3+t5x/uq7g+Xhw8IGkXqwn04w9/dzVoK3KlWZse5tjUhM"
    "dI2gyJY+FP6j6kN7daFgTnP6OG2k//Lg1fn64mxLxrYEhn/z8OtxIvTV1aTi43kJGmZ56Ecv"
    "EP1/n+9dib9X84uZTj3f22t99Vun//7yl7vrc24svS1hvTo7v/xucbe+ebN48/b2bnGyHixc"
    "w+naH+qry/eu867I+t3p+vrOLO5XvT3T7T7Xjr3awJ4sn0wq70f16vzm9u6rq8v/bUd1MFVj"
    "fnx93pv0J4U+n+PE9nj4xZ6hDdKAYp99tg+BeF5O+/rFFzMd2Yt1tvbRYX6HjXfyl/t6MDjG"
    "Hlf7/C49w7anEw5sQbB7dbY5wZtT3/Z207TZvZPmDpPzb3f78+FBLXOGTXMsmuf6hznFrN8g"
    "sGPrsOOmvW/PntsynnoZ3z4Utpo5v9WS+GnNjCv5UY18t76rqSnsKDOPavK+sbr/WU3fPqpD"
    "E7g56c+V21Vu2J1eKl+uprvd0BPj3MHhHpVnqGif3jT88aEa+lb26Uu3b3onzk09lbYdDk23"
    "9ovVzXc731hX5j/quzrTkuf7b2dq1N+2h/SyNxS+2C1+NDeSb4/2jO/lysnd0fw43N+Cb/ey"
    "6+2b5vzVq7171v5xbHFwIKqqvR85YdzPtftx+qaB2W1O47BNbbf9F+ezHJg0NGmhb3HfKB7u"
    "APSnbZPOnNi8OHeM2TQ/O5Gu4vv03h31/WCjzhz5zh0+aguYBb+3pyvXp22VzKgPA5zNabtv"
    "3aOm4GNWvdmg9p6Mk8JuqGdvh91tXua3DElz0rjXYKS6TQwwds/Ug7lz2E2QsfejJ1jN/rQJ"
    "lsp+zxa/s6X3ho5Hb9EPTHvP0tVP26dXo969qh+a5ZOfsJ0P5ce6T+qfK0G+n5+NtT6dQPM5"
    "kdI3s1K1YdX20F9sK2lj1X+xqv/iqj7xVf9lXj8chdZ1+C8P7V/TwkezQm5dPJpy8sVfJsa8"
    "ffrj30/eq/f/hVajv5ux6OFF9TffDN1MSxv9r90VOSP/NVvjureGrO7W5d1+o1A/6Jv1rdl0"
    "ls8fY9RyKP6fe+B+swm+evfvS45v73RPEcLB8vBnz/qmn8G+fvYddDqDjwxjdfcDu2MnFu3q"
    "9DUMP3uW1Wast1vWsZk9xwrtE4iBF1u/i4X24bzsqN05BLdHzIMHxPxRY5ndL4/07eHRLFC/"
    "T/o5xD0myV7IH2GWtGJbpsnoH8w0eXJ+ubpxE3Cz1zQZq+TZ+vTq7J5yicqt3w1Tc7W/ZOCt"
    "nRdXp6t7C05cxE6pc6waPOBeUsZK3N97pWSs8Hj419oXt3nsV+qnY/G5cJC3Fxf0ZY3S+nLY"
    "lY82bQ1dHn973hf4nH8doc3LLblmj6eVvAxevKT2AV6uexMq+Hqsf2t4R6hxC0fapPWfj9N3"
    "bP86GOs+njBpc0YPX+45Onc3nkGQ3jeru9Uz3+rxyfu79e3RTtGzSaE715HdMqev315+f7sp"
    "Zb/vlut9cG7gm4Ii7Jb0PHq2z6J3df11HxtSX924BfxsQf5cX59fftf55brh9vHZ+Zv15dCe"
    "O6WO9tX7+/WrR1S73F/HydXd3dWbn9W9Oeq9rT260z+hZs/j7vzSyeKfVn1ApJsQ1OBL3MvL"
    "ez/vCzw0rv0VbMo8OBPlxfl3l268d3P1rLb+uFvbhynpw7zVdrIzfdBGPBw1f7z2u57Js/mj"
    "Hal0Db657oP0n6zsn5UDa0+OPvmAzbS/RMLQvU0oldPefjh3K0n1+w2g3xO2/jQoPT9cOe28"
    "19AWO3/tr3hoGH2/r0b6dAQ2cJ1l36/f3x74kg4d/+eHwxklyBXgxqTyL9x/vx0iayYdMXKP"
    "tPG7tnlteoPC9/rq7cXZl+aC/8IXPt5h6zDqCWdR977i5tnveofAk02Tdzfvrb312fCnh5u9"
    "urzQJx/f6mAixbl/7P89Lw/ki9OnfOGzwRQ6fuuQwbmGYn/wv23aHdW54SDfGfvvNhU827TC"
    "o1sV/PKXi4O/3+wcTvWCB7q84cazsfuTcJgtrfLDJ/3Cg7bY/zqnRRp9S3mM/1FDh/dqcEuV"
    "/Lr9py//+NXLb778360rmRvxD19+9bL5X1+Vf/iyfvl1+dU/9X8KY2xSl2/fVOu7H9fry4Oh"
    "+aPFm3N3lL1ZvdsKDRz+6jQw92c3Kf3PZ6L9ti/d09xP0fpZ6P3A59+vEfx88vZusTrt/b23"
    "i9XNyXkfaz7Ad/vo1nr80wOlFVu8NxL5wThpBT8/KjiZkveTA5IJkx8VJfzYEOGefz8pTNhm"
    "aReBTWNkx3bu37/sW79weZwx8Ly/MTllpvtHPO31lmnqD6sLdzK9cWt/mKJrJwzu33dXm711"
    "jIywGelv+F3Z+XL+ZvXdui/73Y078E5XF+txmxwpfzp/t77w7u9ZMXr+yU7c8tKCluejlSeB"
    "0UsLi74nIHrYtBV3/+LgoJ+scZ4OjT2fLZZbBtNhCA99FMx8dPLQR+HWR1uMQvj18jgMwtR9"
    "d9N38DgLktD98t3wyzIL+19ODuf1rb4nr6/c7F1d3q0uLAa9vno7OJPdHLw+Pl2fX2jZ/Ybb"
    "GjSJfobPT/d/Lfna9/nJhdtD/3R1vol90OTP9utorrltyZgWscnfon0+V892qT3i5Dp2N+mZ"
    "tbBL/nyetzNF56WxN6JNjIT+D/2W/8Xiy8vhxvj7mb+v3u1+OLccMCWza2Lfgpt8925/3Mhg"
    "I+7l9fdv31zdng/3VbYF2W3UB7u8+/XkEHULY3vmd0q8nwmAkW918dkX027MlzS2DpLr/nkw"
    "nL6Tz/a0YOy271bvDtz/Hv7uwz2xQoPDxo223zS/GPrvDt2h+uurHw8w6qNFuO0U6Xf6vj9P"
    "h9F8/sWu2jE3V26v/vJV3+75ylIUnPchYYP59sTBvO/7W+MXVz8eLdyu//bNejjFetpKfx7u"
    "Tfba+uJiWOxOZPrf5to5W918b9y5PXYi3Ls37hanfYj/j+vF2dXi8upu8WMfeuaOi7e6jCtm"
    "9M33F8N/7FX0xdl5D3uGrsy147rbh7E5JO9U19XNemX3yYftZrhP9+Pr87u1euL0SXecXd6d"
    "ry7cGIb7VX1E3HDSXTlV6eqV69f57fp4pqW5xnVXUwx0LV73G9ywlBzpVly9HY7P9eK1O1rt"
    "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"
    "9z/+rf/xP/of/7P/8f/0P/5X/+N/+w8W/W//V//jv/U/ft3/+Kz/8bT/cdz/+E3/49mT6RUP"
    "OzDIpb/PqdFHqwfuf9Hf4NDYORJ2Nogf5g6E7bT3W4v3hz7OL5lZuj+4hRsnswt3v3S9WH17"
    "3A+g/6XsnxA5ukcUX5xsFZ5ZDfc25dbQvbXPbejh860EFfdtGqsZhqaHP5cnP3WYH7EHbAl6"
    "rzX9fQQ8d8Kd9v/7Gwj4/rf4dkweXki3687nJ+dkso9O7Rx+CoxT//b1l/XVm+ury/4FHKvh"
    "zeqaLkPY29ym018tczvp9HQK0sPD49u3J653B0/Dw+cO4R3/5er88uDJk8OHLSNOy3i1ckt9"
    "yJdrvfrZAjBo938/CRh04/96Cfh+br+LDnc1Sd6A/N5teMt3tTvhP/+8Dzp3wOJ7t9UNpK33"
    "lPtXuN4F3a4mddoLzPJdHsTL5497l1Zf1Hu+gKyeLn772/7m2+nil+6DrpvZLUzMjvs7JrU2"
    "loMt1H88+f3F6beHP38XUQ7pmVuQLx355Ul/q839D2ntbXJkZIYF43j891DnBhX9efTaRYtf"
    "9eJ0+6sxIeOgS3tr4yJ4WvRBEoPDajEcNMHyaZj2tDfrs3Onxx247byPSg+zp/FyyEk6OAEP"
    "wsPjT7ZesvphE9FeLH63WPbp0DckV+vvFoGjhTMJ+6cbyTO3YI6ms9o79Dck/3YLaeM7LZP3"
    "WT5MTn2J+eoHtzP0M+oUYqcBxNsr4o0ZPR5Sc62YO/E8pD7e2DP22K73P281qt+7tbb1l3NG"
    "x501e7j/2W5rV2/cKLhi/v2T99frZ0PTfbPzL5Ssbm91rcnM1M9m0NTMaySHD77V/Q8+qiD+"
    "P3JYYfBxw5pNaez6Yb642/X6ze3gM3l7fbc++68e5NPgp4zpUctQhrE5D7Z0zq+nHpJZS9rz"
    "GYfTMVDrpp6BOi1+MPjf9dGwLx4OrDteXfev0vUHyLSKE/gefxLnwXUNY5eftlXv9vlo70N/"
    "j+IztfjZd1Xx9y2O74ei97J9t8ZZ3p/cy3t3cM/U87eYAI5q3yzsGcLPnAra9CegwdG3WL8L"
    "ju5l+aaGWVaf3svq0yN+/7dgcd/7XdZCr2Nb++Zga0w/k/cD2Jhj/uDW2eL+DDK5l/2oY5b/"
    "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"
    "/Rb+HsTcWJLfbWFvlJ+366eyi4/dIp+LNKJftg39NI5I477fQtqP6Cf/88SYehl9Hfppv0oa"
    "0U9j77TvWXTd38fUk98D/VyPRvSb/DzG1G6zR/TTPv00Mn81O919TKN09z2LWhdKE/qJxVMd"
    "U/erGCdN+/ypvlsdU29jJGk1wSTa25gm9twNzkvE5nQT51natu/5lC4Te441r5cm9twlvxs+"
    "xm43Jub/ovPHnrHg98WfXb6baWZMqLmbNDMmdJ88o584Ks3ol21j/llj/jSjn/Yxpn3Por/b"
    "jH4uazP6zX4e+mmNKs3o5/b5es+ivu+MfrM12OOpO3/opzM+0gyTaH9xWvb+SPlbiA+mteO0"
    "n2Hq9m8/w1RrJ4kx9eFgG/ppTJ0W9NN4N3GG6UG8mlgDOmhOJrEG5Ni8aUE/7Y9IC/p5LLXs"
    "59c5DfYsHpyXfc+s2hfWgA6uC4ypvTc0MaY+uO9Z9zNg9d3Ys3iQ72tiz+LBdXr3sXMfwJ7F"
    "g/a0JfYsOu5hWtEvOV30y06jcJ3zzJyCx5icCWM/zcyZMAeN+TNnwhw07sysAR3UZmfG1IfJ"
    "z0MDjZsya0Des5hZAzqojGfWgA5qYzNrQI4nl3s0WPw8NFj9HmiwOg3OxTn4OuZVND7NnAnT"
    "HPxu+PnLvyfjY9dovJHxsWtUBzMxN+wfkPcYkaozeT/DtPN1nMGpNaXMnsVG45I87OvYo2ys"
    "w2qsktmz2Gg/f97H1FofzMN+Dr3TQL/B74Z+wfcS51Zz3BkfO/tZ533PosbFmTG159wya0CN"
    "6kJmDcjrfpk1oKA2O+9nmGqMnlkDapLzgn7q8zJnmDbqozJ7FhuxRsbHrtFeocyexUZ9VGbP"
    "ov1JMnsWm+Lr9jM4/W7oJ5+fzJkwjfabZWJENtrPleM+r6I8EyPSZyFlYkQ2biPYs9hoDJfx"
    "sWt0JnTGx65RLIjMnsVm8nXo5zaHPYvN7Dyj3+J0J+ZZ/bx9z6Kfh35uS/YzTLWnPHOGaeM2"
    "gjNMG/VHmTNMW3F3Zs9iq7W7zJ7F1nWfM0zbg5+Hj4T2QeW0r2HoW7JnsW19L/MHmjvM7Fl0"
    "bN6c2HOivR95PxOmd/44Q7L3e+zxcFVn6hrQ0GquL7MG1Lo9YA2odZ3O6Bd9Hfq5PcDHrtUe"
    "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"
    "CX4eGrTOMxponjrCq14Hi/CqIuJtNjTQPFeEV+37FeHVRfUowquL1lwjvLqo74nw6iKGiPDq"
    "ojWvCK8u6qMivGq/rAivLuKKCK/aVyvCq4vqeYRXFTlvs6Ff8fPQT9wd4dVF82sRXl00xx3h"
    "1WX089BPbUmEVxf5z0R4dVF/FOHVRXU/wqvLbBv6qU5HeHVRnY7w6rL6XvRbbEM/zZlHeHVV"
    "PxPh1fXg96j6rY1tIzaVNXh1lQ9MhFcdPyrCq2vj91ixHduICK+uYvsIr67ingivrmKICK+u"
    "4o8Ir66dnzdgU57hVfvnRXh1VXsV4VXHy4rwqv34Iry6us2BV9feeUa/3s9DP62NRXh11Tmz"
    "EV5VZMDNhlZuS+DV1W0TvKoIYpsNrbS+FeFV+7BFeHXV+C/Cq/Zri/DqOuo94NVV/WqEV1ft"
    "hYjw6rr43dBq8b1otTjdhXOjnC5lTXNVsfJqPrjcs75q/7IIrzqmVKy8mh1TKmbOq3Kdqbya"
    "7fsVK69u6Uq/yqvbvSoHlVc3m8pL5dVsf7VYeXVLQ+9beTUfOt87YXP+Zmx+X8qa5vBi5dV8"
    "EDvHgn7uLyuvZseZipVXs/3QYkE/rcnFgn6uMwX9XN8K+onFY0E/7b+KBf0G34t+wdehX/C7"
    "oZ/72sqrORTfi37uf8te/mxDP/F5HNHPffKIfmL2OKKf+9UR/cSNcdzPS9N7jOjnPI/o57o6"
    "op/7vBH9tK4WR/RzvRzRT2wVifbcetw0Uv7c51Vezc3Bz6v6Ne5nKq9mx9qKlVdzo33msfLq"
    "ZpN+lVdz43Fi5dXseFmx8upmU54rr+bG7Wnl1Wyfrlh5dbM5f1W/JvrejM15rvo1HktVXt1s"
    "ft6EzfeiX7EN/Yqfh37+5jP6eSw1o5+/OfFsGp2TFWf0G30v+mnNIc7oJxaPM/ppj1Kc0c9j"
    "JHi1mf089BODxRn9Zr8b+i1OF/1c/mb08/hqRr/VGqCf9hrEyqu5dZu9oJ84KlZe3a6TfpVX"
    "s2Nyxcqr23VKt/LqZtN7VF7Nrdu1yqubTX1A5dXcekxTeXWzSfvKq9m+RrHyar5Rfyuv5jb7"
    "eVU/x/2KlVez43nFyqtb/vQtV/TzGKTyanasrbiinxliRT/tK40r+mnuNa7oJ7aPnLfkmFxx"
    "RT+PVVb0W3xd1a87+Hno5/FB5dXcaV03Vl7NnfytEv6rndq1VHl1u66VreW6IlvHdZ1sVYOu"
    "83UDtkG2gG2UrWrQaYyZKq/mTgybejSIvq5wnd8NDdTGph4NRj+vlqGu+HkLtlm2WoY6MWIa"
    "0E/fMg3opzYiDeinM7/TgH7aa5AG9NNYNA3oN/s69NN+izSgn9ZN04B+asPSgH6Tn4d+i9+t"
    "6tf7m1de3Wy+bsbmvFT97AOTKq9m+6ykyqvbvcpf5dXca+yYKq/mXuszqfJqtj9Eqry6XdfI"
    "NnCd3qPyanbMq1R5NfcaJ6bKq9t1et/Kq9t1zkvVr3c5DegX/Dz0U9uZAvoF5482TPsnU0A/"
    "nVeQIvppj1KK6Ke9Cymin8Z/ifOBe7VXKaKf61ZEP+17SBH9NHZMEf10/kGqvJod+ydVXs3e"
    "a58qr2bv+0+VV7P386fKq5tNZaPy6mZTea68utmkX+XVPMhvLFVezfYtSJVXN5vyV3l1s0k/"
    "zgd2nJ9UeXWzqWxUXs3eu58qr242aZrQSr48KRErWf1+Smjlup/QanL+0Gpy/tBK/gEJXh20"
    "JytVXt1sShdetX9AYn110DgnZfRzGwavDhq3J3jV8XYSvBq0vprg1eB+C151TJAEr4beaWRs"
    "znPB5nvZz6oxQ4JXg+sRvOp4O6ny6mZz/lZs+h7wqn0GErwatL8pwauOhZPgVcfRSfBqSE4D"
    "/VwX4NWgcVOCV4P2HSZ41b4FCV4NrgvwahCrpX0/8Og8o5/7WnjV8WwSvGq/hASvOsZNglcd"
    "4ybBq0E+mQleDRrLJ3jVfgkJXnXcmzTu++FsQz+Xe3jV++8TvBpct+BV78lP8Krjz6TKq9m+"
    "BQletW9Bglej+1p4NR6swcp1+kbwatTYMcGrUXM8CV6NHhPCq46Fk+DVqLWEBK9G8W+CV2Pn"
    "56GfzllM8GrUXEuCV6P7X3g19s4z+okXErwa3R7Aq7F3XtDPdRpetV9CglejOCXBq9H1F161"
    "H0GqvDrYpz/Bq47Lk+DVqHXJBK86tk6CV6PWSRK8Gt13w6vR/TS8GrU3IMGrMfk90C85f+in"
    "OZ4Er8ZsrdBPbJXg1ei+dtn35yhdeDVqzjfBq/YZSPBqdFsCr0b3jfCq/QgSvBo9BoZX7SOR"
    "4FXH4EnwatSZ+AleXd2nwKv2QUjLfj6/r0M/9/HwatQcQIJX7auQ4FXH9EnwatK+3ASvJtdf"
    "eNX+AQleTfL5SfBqcl2FV5PrILyarAG8mtyvwqvep5/g1RR8b9Uvabyb4dWk+pHhVccSyvBq"
    "0txXhlcd5yfDq47zk+FV72/P8GpSH5XhVe9vz/Cq98FneNUxgjK8msT2GV5NWkPL8Go++Hkz"
    "Nt9by5Bj8GR4NYvVMrya5cec4dWscpDhVe/ZzvCq96NneDW3TmPApu8GrzrOT4ZXc+vnVf1y"
    "53QzNl9X9ctq2zO8mtW2Z3g1aw4qw6t5cP7QT+U0w6tZawkZXs1ixAyves97hlcdSyjDq1lj"
    "rgyver93hldz8b3oJ/7I8Kpj/2R4NY/OC/qpzcnwatZcZIZXve86w6t5dv7Qb7Yu6Lf4XvRT"
    "XLIMr2btZ83walY7meHVrDXmDK96b3eGV8vBzxuw6X3h1aK1tgyv5sXpVv0cSyjDq6V1Xqp+"
    "jvOT4dXisgavFpdTeLXIpz/Dq44RlOFVxxfK8GpxuYJXi8asGV71/vEMrzrmUIZXi5gkw6ve"
    "753h1RKcLlppPJQTWmkdIsOrJTgvaKU5jwyvFpdxeNVxfjK8WlzG4dWiuf8MrzreToZXi8su"
    "vOp4OxleLWLiDK96n3SGV92fZ3jV+64zvFoWp4F+Ooc0w6tl8fPQz/0CvFpct+DVsjrP6Of6"
    "Bq96D3iGV4vrG7zquDwZXnUMngyver93hldH7a3I8OqoeeUMr46aQ87w6ii/iQyvjlpjzvCq"
    "92JneHXUvFmGV8fW+RuxST94dWyd55nYCM5z1W8Uk2R4dXT9hVe93zvDq44HlOFV7wHP8Oro"
    "vgdeHbU+mFlf9V7sDK96n3SGV8dkG/plp4F+ml/L8OrofgtedfyeDK+Oml/L8OpYnGf0G51n"
    "9HP9hVdHzYdleHXUHECGVx0zJ8OrjreT4dXRdR9eHcVlGV4dXVfh1dF1Gl4dNWee4dXR7Qa8"
    "Oi5Ot+rnvecZXh3dD8Kro+svvDqu1qrqN2ntLsOrjv2T4dXJY0d41bF6Muurk7giw6uT2wN4"
    "ddIcRYZXJ49F4dVJ+74yvDodnL+q3+R6Ca9O2pOV4dXJbQS8OnksCq9OHtvCq5PmKDK8Omnf"
    "ZoZXJ9d9eHUS92R4dXI/Da9OmlvP8KrjKWV4dQpOF/2i00A/7fHP8Oq0+F700zkYGV6dXF7g"
    "1dnlAF6d/S1ZX53c98Crs5kEXnUslwyvzm534dW59XVVP8fSyPDq7H4BXp3dp8Crs9tieNWx"
    "YTK8OrtMwquz1oAyvOr4Lhlenf194VXHd8nw6qw5qAKvzpoHLvDqrPniAq/OGksVeHVWH1Dg"
    "1VnsUuDVWfuBC7zqeDEFXnUslwKvzlqLKfCq46IUeHUWnxd4ddY8UoFX52zbzPP8bpSh4rxQ"
    "hjSHXODVWYxd4NVZezoKvDpr3aXAq469UuBV778v8Oqs/qPAq96TX+BV7/Ev8OosP/UCr84a"
    "TxZ4dV6dF/RbnC76iaMKvDovzgv6rb6u6rdoHFbg1UVtZ4FXvYe+wKuL1hELvOr4HwVedRyO"
    "Aq96b3eBVx1zo8Cr3rNd4FXH3CjwqmNuFHh1yc7fiM3pTticv6rfUnzvgs3vhn4uL/Cq93sX"
    "eHURp5S4z/kqL/DqovW3Aq8uWq8o8OrisgavLlq3L/DqorFFgVeX2Wmg3+w00G92ntFvcRro"
    "p7W7Aq8ui3VBP5dJeHURfxR4ddHcZoFXF/FHgVcXrZMUeNX7zAu86v3oBV5dXBfg1fXgNPA/"
    "Uv9WiGcTRl9XtfLe8wKvrhpHFHh1bZ2XqpX3Zxd4ddX+vwKvrppHKvCq46IUeNX7qQu86rgo"
    "BV51XJQCr64uk/DqqvWKAq86LkqBV1etNxZ4dRWzF3h1jb4X/dz3wKtrdLro5zoNr3pfeIFX"
    "1+J3u9JvSGK1Unk1ZJ0hVK73A6tcEc+md19beXVoXI+IZ9Nc7iH4869+8pOf/OJnP/vZL07+"
    "/Mnj7W8n+eRnv8Dy6MtXz7+4ePri+aOvXzx59ezs8cnZt9+8OL94+fjks8/+dPb5N6df/Mdn"
    "52d/ePX0/Oyzzz45+c+f/OSnr16enby8OH/6xcVPt0f/8+e/P/vi4tMnZ18+fX72L+cvvjk7"
    "v/ju0fVTfvrZZ2cv/+nq2T99fPKfJ388ffbq7JcnF+evzrbs/Oonfzw9P/mHs+dn2+P+Yf2s"
    "Ofn1Xek+am5f+S8vnn332qvb7er9vU7OXz1fXn3x7OmTs9Pn5dlXL86fXvzu60dfPj179uTx"
    "yenjk88fn/zr5XtdSneZxGenv7r6+y9+cVJevnz19dnJ6X97efLk7Kvzs7OTpy9P/o9fn3z+"
    "315eXfL0y5NHp5/Wnx59cvL3J59f/2N/4uX/fXa65fM3Wzqnv93+d5nn098ctr9+Xv/a/LYm"
    "+OfrLJz/4+nLi+1HcnJl2v75+fGfF1xx9Rqf/q+z8xf67dr+4vnZ9bv866vnJ9c6nJzuQpy8"
    "en7x9NnJ+Y1XfHb28uXJxe+26/71F+3VA/70u6fPzk4enR9fdpPhX09+cdL6Ra/zTu6u/v6r"
    "Gz9f6OeLmz/vb31+NF1feTRtrzI//eP2Fkrp8+/qPx6f/Gl7p5M/vHpx8fTs+cXJ0+cnfzg5"
    "ff7k5Pzs69Onz5+cnV+azq+fdfn9rm789OnL/3fT8OZnI7W7RDt9dn52+uS7k4uz86+fPj+9"
    "OHvyf9647fzs4tX585Pnr549O+b8z8c33bX5vj5/uOOj7r89OXv+4iq5F+f/uCX/9PlX/76l"
    "vz/r06/OLqYXZ19++fSLy5fnxa7L461HPbv4H8//eHa+1eQ9vaf134/uTkX331kWbqZ28rd/"
    "e/I3569Vtb7M5aXz0y+/vHyB451/d+tRv/renS+/OH12zPbXr55dPP3m2Xdbfm4LcHz5x3rh"
    "W0+8FPwPn54+efLP5//26vOL89MvLmrj8Onnr54+e/JPLy7VOH326JjfxzUHn9x60NVHvfWg"
    "+ip7Fsfv3u5px4JycZW5/f7LNu/RVZn55FY617VKT7kq3G/4Rrc/yhvK7LFlevn0q69P/33T"
    "5axcXH7by7p5W/cDmbjMwPev//WvTw5O+nvJHhN7+poSevuhpMeTfnMU/BF3bN///PvGrdn9"
    "87Gb2HqvJ8v5+VboX3xxeml5ufcRZ9fWF+d7zrd24d9/t7WU2/+fnjzZep0vLk5Ov/nm2dN6"
    "78mLL0+m321qbM3qy7PT8y9+d/1Sz199fZXMy+21/ORbBf5SPF26qdbcodpvbjzh1ndoPvl+"
    "x3L2clNhS/n52Z9Oyvn56XfHRD459iBXT51ebF3Ddumh2r98cX7y6OqrbLbmV9t//p7P8vLp"
    "/zq7rPC66++Pr7ld+fOfO+uXb3Yj22eXQ4KtDS0Xj55+8v0CUl/3Mt+/Oabw2+8Viqe3quPx"
    "2p///O7yfJ2Rmue/2RI+ivG/L6GYa9buLkr/dPrV86cXr56c3SxLS33hF+cuW5cl7o7SVcvW"
    "s++uStd3W2N8sr44f3723Vawtr9sZfr0WDdvlKjL53367Oz5Vxe/+9Wbvv/+3W9830P9vt//"
    "eJcXfPv0dV3HzdR/8/S3n7y2A7sqRNc/Xif++5r472viv7+Z+P7Rnl59q9/f/uny/26mcKuH"
    "0I+PPea82Zj+w/qoeXz71ltv9vtt9HYtwye3e4I/39Xl1xL89Lffz9bNMuHqcEzi8es76U9u"
    "tfn1wq8uX+/y5/F0+1B/84ZKdVeWrn/ya95di15fDZ6cffHiydmjz7+7ONs44OJPL/7Nw+wX"
    "ry6+eXUxXv5Iefy/nz6/SNOz06+/OXtSy+bVrRRiktdtn748u6iXqOm6ehEe6E+84cnplsFH"
    "h28PTTM/PmmH8HiT5FeXde3b/y+d/Hz7s7/6s7v6s93+bK6f+s3W895+aCWQ6wdTwZU/5erl"
    "d8+fnL/4+kzN8+vf+kqpW43x5Sc6ffby7FZdfVmry8utulzetf3t+9WV8nT5RX599SIuYfW7"
    "b7D26OX2wncUHZeuu97iN3cZ+WTbWK7Z/vfysoAdc3GztCp3dxbS/e0vefH1zfjfXF12R6Ot"
    "z3G7L9wz/nbf9a7X9Ae+HJL889dnX13i3RuJ866x5eW3e7x18MdkqC+3BlIkcNVRPXv2lqOo"
    "m03XVV1/3UjnmMjGp0r89iPePf1jT+gMfL9/VA6a336ve3x9T3V3v3dX7/XNi5dPqQxuS26U"
    "2PqNnr346o092qUy10/7+7va1zdCoFL/zf6Yy5rypo7pznse35b4mM0bTfSNuvDnnzBD82lt"
    "preE61++N1nUHB7QbNGe6f9n+dd/+x///D//7XJi5eodNb58/uWL8enFy19eKf/4+PG3TmzL"
    "+f989fXnZ+e/PGmOv5w+e/rV86+3Gv0vpxcb0T+ftr9uF//y5De/PV7ESPH8cji2PeYfz/54"
    "9uzykhuf/PsDkrMvpk3UP704f/LyX87Ox2cvvviPX57Ex3dcd/Xb5RP/83IYuv9rG4o8Ob04"
    "veMhTT75829vPujPj/+S3Oz+a/fMTvihstP9INnpfqjs/CAf643fil+wvHNJbt+mJG+jnSa9"
    "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"
    "/13b8XAPG713e9Tfuz1qPsr26A2Drfbe5y78155h7j/spE7bpvH1y11d/5ctu1+2R+G/MESF"
    "v9r26OMcH/X9R3kA6f3nL+/t29l80E1AbRfz69e7uuEe613bH8ODnWd+0y6J9t5+ng/ZYewN"
    "b/6W88xvWAV5yPPMXf4r9Ri7/w7p/sMuwrf9eHj9DukuvEMw7asdQeG6Ubpsnob+I/Qde8s2"
    "6U2ufQ+5UQr3bZO6j9OLtQ/3bpMOH2ebdN8mKXxYJ9Z26NvXb5bu4l/mxHo52XzZLg3pY5xK"
    "+i++b/re7htN/3GiW/5rbZP6j20qKZTwes/6Lv1lHq2XjdJlyxQ+ysM+3vJs3fRxNkpNd99G"
    "qWs/zkYp/ZXCW3/vkVL3YcONt3HoX+9m3+V32bTYs0moNkqX8PZwA/y1h7/Wkz/6w33pLX6c"
    "8Nb9lbrcv2mL7VvSW/ywbVKawutPMOwP99gDsP0RH+4sd37XFuWdFt4ecOCt5t7HgLzB8ehB"
    "z3Lf39XsI53lvn+j9D5muX/7q5/85Ce/+NnPfvaLkz9/8nj722Uc5p/9AtOjL189v2onHn39"
    "4smrZ2ePT86+/ebF+cXLxyefffans8+/Of3iPz47P/vDq6fnZ5999skm5k9++url2cnLi/On"
    "X1z8dHv2P3/++62l+fTJ2ZdPn5/9y/mLb87OL757dP2Un3722dnLf7p69k8fb6/7x9Nnr85+"
    "eXJx/upsy8+vfvLH0/OTrZX8p9Ptgd9+1pz8+q50Hx22K/ecnrz8w6vT87N/f/F/vTp9cv70"
    "2enWPp4+e/TNJt43W2v4zdYpftN/wme/fPyTb7vtsd80n3578nfbJdt/fr5dVf/Vf/rtr44X"
    "fseF39ULv6sXflcv/K5e+PTLk0dXT/z1r08OJ3/7t/Wuy39saZ784hcn5ctLJa6/xPnZxavz"
    "57dK4WnT/LJm5e+uMvb41q/tL2v6V79+d/vXbutSbtnay+d11294+9f2l9cv0n7vee0dz+su"
    "n/f9jHXtL+/KUdddLhtf2/5clfrz1Z9b73Omt68f5PI776/f7Z/g+HN7+XP/up+/4+7v6s/f"
    "3f6Zu1/z89nzF18/fX568eJ8u+wyJz+7uuXvrpK9/Htz84ZN7u3Cq09++8ruk5Nf+IG3bmy5"
    "sSbRXd1YH9K89sa3Ki1bsbzM1c/u+tS3io4vvascbX/cVZL6G2m1Vw/o7y5W/Y209kvvKmOn"
    "bfdeStmfj03DH9wk/PuLf7tqKV7TNGxV9b+fnW8t3sXvzk5On/z+xdPnFycvz87/ePby5PTl"
    "lfXp88uR2tkvr1uIl1urs33Ut2qBava+90GvPublcz7d9NvEqn/tLosH1u5o9R7yyy9bzY2v"
    "uL6v8dN8X/fL71/RHtPT024cpd0e86n0mmM+m7vTa4/5bO56P+feHjttd8f7tcf09LQbhwd3"
    "x3w2d+lilZ1e196hS3dneje+Q9fd8X7tnd9hD1ywFVUV0ounX5+9fHT6+OTzvSjeXUhOSeHz"
    "q//+/PLf7f7vtv672//d3Soop+TD97a37m3vurer93a37u1u3dvdcW97M8/trTy3t/Lc3i40"
    "ynN7K8/trTzfvPdmnttbeW5v5fl7hUd57m7lubuV5xsFvbuZ5+5Wnrtbeb557808d7fy3N3K"
    "837vzYJ09u3F+ekXF4+efn361daObSPF00u7xz5/2BrBrcG6u138z5Nvt3Hvp8Pjk++u/rsN"
    "F0+ubPuTPn2yFdXnl6i6le63vjBw4et+vL67e9OFV8l88qu3a3qvn3Dx4pt/PPvy4ijGpeVf"
    "n371O5tuM7Z++vzFxcWLry8foaQ3lZ+//PLF+ddb+rXyXmbm8ZW4uuzrqzHsdo3Gs59e//3T"
    "L87PttwuX3+zDY+//8aP71DhxrO/+ebp86/W/dP/+uS6FDz6dhPxk9tDrBvDnOsXoMRd9uqy"
    "XZW6/7+9Z+1tW0f2+/kVbIHbymvFjZ0eLGD3gaSP3QJ7zyPN2W1QBIFsM7E2suRKcmwVm/vb"
    "78zwTVFO0sfZXewWTSNR5Lw4HA6HQ7Zxyw4ObnFKoP8iG+4wAHcYgOv7Pu4k33hQRwGoowDU"
    "USdU5SaIX1CdRSigBsSyP4FfzwKCh/J+3xapbrYVzbYdzbZuM9Ub27/iqgeaIiv7gx8nrRqN"
    "qtF01aiKdTnjv6RbnkE1Tx8igSGWcHpuc6GYg4rXpCsxI4MxuIR30M3F4CIrijKyEAy2vZh1"
    "fGp6PQv8jeXpt9RE4B3L37FV7hA/9gssWyfXkQNp6oBz+dRe1Y7+xVa1/3v44fztu59evzn+"
    "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"
    "CnTU/wMGlXYa"
)

IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff")


def _jsqr_source():
    return zlib.decompress(base64.b64decode(JSQR_B64)).decode("utf-8")


# ---------------------------------------------------------------
# 后端 1 / 2：本地库
# ---------------------------------------------------------------
def _decode_cv2(raw):
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    try:
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        det = cv2.QRCodeDetector()
        # detectAndDecode 对整图；失败再试多尺度放大（小图/截图常见）
        txt, _pts, _ = det.detectAndDecode(img)
        if txt:
            return txt.strip()
        h, w = img.shape[:2]
        for scale in (0.5, 1.5, 2.0, 3.0):
            big = cv2.resize(img, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_CUBIC)
            txt, _pts, _ = det.detectAndDecode(big)
            if txt:
                return txt.strip()
    except Exception:
        return None
    return None


def _decode_pyzbar(src):
    try:
        import io
        from PIL import Image
        from pyzbar.pyzbar import decode as zdec
    except Exception:
        return None
    try:
        src_obj = (io.BytesIO(src) if isinstance(src, (bytes, bytearray))
                   else str(src))
        img = Image.open(src_obj)
        for d in zdec(img):
            if d.type == "QRCODE" and d.data:
                return d.data.decode("utf-8", "ignore").strip()
    except Exception:
        return None
    return None


# ---------------------------------------------------------------
# 后端 3：浏览器 + jsQR（零依赖）
# ---------------------------------------------------------------
_DECODE_JS = r"""
() => {
  const img = document.getElementById('q');
  const W = img.naturalWidth, H = img.naturalHeight;
  if (!W || !H) return null;
  const scan = (sx, sy, sw, sh, scale) => {
    let w = Math.max(1, Math.round(sw * scale));
    let h = Math.max(1, Math.round(sh * scale));
    if (w < 24 || h < 24 || w > 4200 || h > 4200) return null;
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, sx, sy, sw, sh, 0, 0, w, h);
    const d = ctx.getImageData(0, 0, w, h);
    const r = jsQR(d.data, w, h, { inversionAttempts: 'attemptBoth' });
    return (r && r.data) ? r.data : null;
  };
  // 1) 整图多尺度。截图里二维码通常占得不少，这一轮基本能出。
  for (const s of [1, 0.75, 1.5, 0.5, 2, 0.35, 3]) {
    const r = scan(0, 0, W, H, s);
    if (r) return r;
  }
  // 2) 大图再切块（聊天截图里二维码只占一个小角的情况）
  if (W > 500 && H > 500) {
    const cols = 2, rows = 2;
    const sw = Math.round(W / cols), sh = Math.round(H / rows);
    for (let i = 0; i < cols; i++) {
      for (let j = 0; j < rows; j++) {
        for (const s of [1, 1.5]) {
          const r = scan(i * sw, j * sh, sw, sh, s);
          if (r) return r;
        }
      }
    }
  }
  return null;
}
"""

_BROWSER_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<style>html,body{margin:0;background:#fff}img{max-width:none}</style>"
    "</head><body><img id='q' src='data:@@MIME@@;base64,@@DATA@@'></body></html>"
)


def _mime_of(path):
    ext = Path(path).suffix.lower()
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".bmp": "image/bmp", ".webp": "image/webp", ".gif": "image/gif",
        ".tif": "image/tiff", ".tiff": "image/tiff",
    }.get(ext, "image/png")


def _launch_browser(pw):
    """优先用本机 Edge/Chrome（不额外下载 chromium），失败再退回 playwright 自带。"""
    import chaoxing_player as cp
    exe = None
    try:
        exe = cp.find_browser(None)
    except Exception:
        exe = None
    args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    if exe:
        try:
            return pw.chromium.launch(executable_path=exe, headless=True, args=args)
        except Exception:
            pass
    for channel in ("msedge", "chrome"):
        try:
            return pw.chromium.launch(channel=channel, headless=True, args=args)
        except Exception:
            continue
    return pw.chromium.launch(headless=True, args=args)


def _decode_browser(raw, path=None, pw=None, verbose=False):
    """
    pw: 调用方已有的 playwright 实例。在 with sync_playwright() 里务必传，
        否则这里再 sync_playwright() 是嵌套启动，会静默失败。
    解码一律用独立的无头浏览器，绝不在用户可见窗口里开标签（会挡住二维码）。
    """
    mime = _mime_of(path) if path else "image/png"
    html = (_BROWSER_HTML
            .replace("@@MIME@@", mime)
            .replace("@@DATA@@", base64.b64encode(raw).decode("ascii")))

    def _run(br):
        page = br.new_page()
        try:
            page.set_content(html, wait_until="load", timeout=60000)
            page.add_script_tag(content=_jsqr_source())
            res = page.evaluate(_DECODE_JS)
            return (res or "").strip() or None
        finally:
            try:
                page.close()
            except Exception:
                pass

    own_pw = None
    try:
        from playwright.sync_api import sync_playwright
        if pw is None:
            own_pw = sync_playwright().start()
            pw = own_pw
        br = _launch_browser(pw)
        try:
            return _run(br)
        finally:
            br.close()
    except Exception as e:
        if verbose:
            print(f"[qrdecode] 浏览器解码失败: {e}")
        return None
    finally:
        if own_pw is not None:
            try:
                own_pw.stop()
            except Exception:
                pass


# ---------------------------------------------------------------
# 连续帧解码：动态二维码（1~10 秒一变）靠这个，避免每帧重启浏览器
# ---------------------------------------------------------------
_FRAME_CORE_JS = r"""
function __scanFrame(img) {
  const W = img.naturalWidth, H = img.naturalHeight;
  if (!W || !H) return null;
  const scan = (sx, sy, sw, sh, s) => {
    let w = Math.max(1, Math.round(sw * s)), h = Math.max(1, Math.round(sh * s));
    if (w < 24 || h < 24 || w > 4200 || h > 4200) return null;
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, sx, sy, sw, sh, 0, 0, w, h);
    const d = ctx.getImageData(0, 0, w, h);
    const r = jsQR(d.data, w, h, { inversionAttempts: 'attemptBoth' });
    return (r && r.data) ? r.data : null;
  };
  // 屏幕这块区域一般裁得比较准，扫几档缩放就够，别拖慢速度
  for (const s of [1, 0.75, 1.5, 0.55, 2]) {
    const r = scan(0, 0, W, H, s);
    if (r) return r;
  }
  return null;
}
"""

_FRAME_JS = r"""
(b64) => new Promise((resolve) => {
  const img = new Image();
  img.onload = () => { try { resolve(__scanFrame(img)); } catch (e) { resolve(null); } };
  img.onerror = () => resolve(null);
  img.src = 'data:image/png;base64,' + b64;
})
"""


class FrameDecoder:
    """
    复用同一个解码页面连续解帧：
      - 装了 opencv 就在进程内解（最快）
      - 没装就把帧丢进**独立的无头浏览器**里的 jsQR 解（零依赖）

    pw: 调用方已有的 playwright 实例（在 with sync_playwright() 里的话必须传）。
        不传就自己起一个 —— 但不能在已有 sync_playwright 上下文里用不传的写法，
        嵌套 startup 会静默失败。
    注意：解码页必须跑在**另一个无头浏览器**里，不能借用用户那只可见的浏览器，
        否则新开的标签页会盖住屏幕上的二维码，抓屏就抓不到了。
    """

    def __init__(self, pw=None, browser=None, verbose=None):
        self.verbose = verbose
        self._cv2 = None
        self._page = None
        self._own_pw = None
        self._own_browser = False
        self._browser = browser
        try:
            import cv2  # noqa: F401
            self._cv2 = cv2
        except Exception:
            self._cv2 = None
        if self._cv2 is None:
            self._open_page(pw, browser)

    def _open_page(self, pw, browser):
        from playwright.sync_api import sync_playwright
        if pw is None:
            self._own_pw = sync_playwright().start()
            pw = self._own_pw
        if browser is None:
            browser = _launch_browser(pw)     # headless，屏幕上不会看到任何窗口
            self._own_browser = True
        self._browser = browser
        self._page = browser.new_page()
        self._page.set_content(
            "<!doctype html><html><body style='margin:0'>qr</body></html>")
        self._page.add_script_tag(content=_jsqr_source())
        self._page.add_script_tag(content=_FRAME_CORE_JS)

    def decode(self, png_bytes):
        if self._cv2 is not None:
            try:
                import numpy as np
                img = self._cv2.imdecode(np.frombuffer(png_bytes, np.uint8),
                                         self._cv2.IMREAD_COLOR)
                if img is None:
                    return None
                det = self._cv2.QRCodeDetector()
                txt, _pts, _ = det.detectAndDecode(img)
                if txt:
                    return txt.strip()
                h, w = img.shape[:2]
                for s in (0.7, 1.4, 2.0):
                    big = self._cv2.resize(
                        img, (int(w * s), int(h * s)),
                        interpolation=self._cv2.INTER_CUBIC)
                    txt, _pts, _ = det.detectAndDecode(big)
                    if txt:
                        return txt.strip()
                return None
            except Exception:
                return None
        if self._page is None:
            return None
        try:
            res = self._page.evaluate(
                _FRAME_JS, base64.b64encode(png_bytes).decode("ascii"))
            return (res or "").strip() or None
        except Exception:
            return None

    def close(self):
        try:
            if self._page is not None:
                self._page.close()
        except Exception:
            pass
        try:
            if self._own_browser and getattr(self, "_browser", None):
                self._browser.close()
        except Exception:
            pass
        try:
            if self._own_pw is not None:
                self._own_pw.stop()
        except Exception:
            pass


# ---------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------
def decode_bytes(png_bytes, verbose=False, pw=None):
    """解内存里的一帧（PNG/JPEG 字节）。"""
    dec = FrameDecoder(pw=pw, verbose=verbose)
    try:
        return dec.decode(png_bytes)
    finally:
        dec.close()


def decode_file(path, verbose=False, pw=None):
    """解码一张图，返回二维码文本；失败返回 None。"""
    p = Path(str(path).strip().strip('"').strip("'"))
    if not p.exists() or not p.is_file():
        return None
    raw = p.read_bytes()
    if not raw:
        return None
    for name, fn in (("opencv", lambda: _decode_cv2(raw)),
                     ("pyzbar", lambda: _decode_pyzbar(str(p))),
                     ("browser+jsQR",
                      lambda: _decode_browser(raw, str(p), pw, verbose))):
        txt = fn()
        if txt:
            if verbose:
                print(f"[qrdecode] 后端={name}")
            return txt
    return None


def looks_like_image(s):
    p = Path(str(s).strip().strip('"').strip("'"))
    return p.suffix.lower() in IMG_EXT and p.exists()


def decode_any(s, verbose=False, pw=None):
    """图片路径 → 解码；否则当成用户直接粘贴的文本（URL / enc / 整段参数）。"""
    s = (s or "").strip().strip('"').strip("'")
    if not s:
        return None
    if looks_like_image(s):
        return decode_file(s, verbose=verbose, pw=pw)
    return s


# ---------------------------------------------------------------
# 从二维码文本里解析签到参数
# ---------------------------------------------------------------
_ALIAS = {
    "activeid": "activeId",
    "activeprimaryid": "activeId",
    "signid": "activeId",
    "enc": "enc",
    "courseid": "courseId",
    "classid": "classId",
    "clazzid": "classId",
    "fid": "fid",
    "uid": "uid",
}


def parse_qr(text):
    """
    把二维码内容解析成 dict：{"activeId","enc","courseId","classId","fid","uid"}
    超星的二维码就是一段带参数的 URL（或干脆只有一个 enc 串），
    各版本、各学校格式并不统一，所以这里做宽松解析。
    """
    out = {}
    t = (text or "").strip()
    if not t:
        return out

    # 1) 当 URL 处理；没有 ? 但含 = 的，也按 query 处理
    query = ""
    if "?" in t:
        query = urllib.parse.urlparse(t).query or ""
    elif "=" in t and "&" in t:
        query = t
    if query:
        for k, vs in urllib.parse.parse_qs(query, keep_blank_values=False).items():
            key = _ALIAS.get(k.lower())
            if key and vs and not out.get(key):
                out[key] = vs[0]

    # 2) JSON 形态：{"enc":"xxx","activeId":123}
    if not out.get("enc"):
        m = re.search(r'"enc"\s*:\s*"([^"]+)"', t, re.I)
        if m:
            out["enc"] = m.group(1)

    # 3) 普通文本里的 enc=xxx
    if not out.get("enc"):
        m = re.search(r"\benc=([A-Za-z0-9%+/=_\-]+)", t, re.I)
        if m:
            out["enc"] = urllib.parse.unquote(m.group(1))

    # 4) activeId=xxx 单独出现
    if not out.get("activeId"):
        m = re.search(r"active(?:Primary)?[Ii]d=(\d+)", t)
        if m:
            out["activeId"] = m.group(1)

    # 5) 裸字符串：整段就是一个 enc（超星有些码只放 enc）
    if not out.get("enc"):
        bare = t.strip()
        if re.fullmatch(r"[A-Za-z0-9%+/=_\-]{6,120}", bare) and "://" not in bare:
            if not re.search(r"[a-z]{4,}=", bare, re.I):   # 排除 key=value 形态
                out["enc"] = urllib.parse.unquote(bare)

    return out


def _main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    target = sys.argv[1]
    txt = decode_any(target, verbose=True)
    if not txt:
        print("识别失败：图里没找到二维码，或图片读不出来")
        sys.exit(1)
    print(f"二维码内容: {txt}")
    params = parse_qr(txt)
    if "--json" in sys.argv:
        print(json.dumps(params, ensure_ascii=False, indent=2))
    else:
        print("解析参数:", params if params else "（没解析出可用参数）")


if __name__ == "__main__":
    _main()
