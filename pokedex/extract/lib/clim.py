import math
import struct

import construct as c

clim_header_struct = c.Struct(
    'clim_header',
    c.Magic(b'FLIM'),  # TODO 'FLIM' in SUMO
    c.Const(c.ULInt16('endianness'), 0xfeff),
    c.Const(c.ULInt16('header_length'), 0x14),
    c.ULInt32('version'),
    c.ULInt32('file_size'),
    c.ULInt32('blocks_ct'),
)
imag_header_struct = c.Struct(
    'imag_header',
    c.Magic(b'imag'),
    c.Const(c.ULInt32('section_length'), 0x10),
    c.ULInt16('width'),
    c.ULInt16('height'),
        c.ULInt32('format'),
    # TODO this seems to have been expanded into several things in SUMO
    #c.Enum(
    #    c.ULInt32('format'),
    #    L8=0,
    #    A8=1,
    #    LA4=2,
    #    LA8=3,
    #    HILO8=4,
    #    RGB565=5,
    #    RGB8=6,
    #    RGBA5551=7,
    #    RGBA4=8,
    #    RGBA8=9,
    #    ETC1=10,
    #    ETC1A4=11,
    #    L4=12,
    #    A4=13,
    #    #ETC1=19,
    #)
)


COLOR_DECODERS = {}


def _register_color_decoder(name, *, bpp, depth):
    def register(f):
        COLOR_DECODERS[name] = f, bpp, depth
        return f
    return register


@_register_color_decoder('RGBA4', bpp=2, depth=4)
def decode_rgba4(data):
    # The idea is that every uint16 is a packed rrrrggggbbbbaaaa, but when
    # written out little-endian this becomes bbbbaaaarrrrgggg and there's just
    # no pretty way to deal with this
    for i in range(0, len(data), 2):
        ba = data[i]
        rg = data[i + 1]
        r = (((rg & 0xf0) >> 4) * 255 + 7) // 15
        g = (((rg & 0x0f) >> 0) * 255 + 7) // 15
        b = (((ba & 0xf0) >> 4) * 255 + 7) // 15
        a = (((ba & 0x0f) >> 0) * 255 + 7) // 15
        yield r, g, b, a


@_register_color_decoder('RGBA5551', bpp=2, depth=5)
def decode_rgba5551(data, *, start=0, count=None):
    # I am extremely irritated that construct cannot parse this mess for me
    # rrrrrgggggbbbbba
    if count is None:
        end = len(data)
    else:
        end = start + count * 2

    for i in range(start, end, 2):
        datum = data[i] + data[i + 1] * 256
        r = (((datum >> 11) & 0x1f) * 255 + 15) // 31
        g = (((datum >> 6) & 0x1f) * 255 + 15) // 31
        b = (((datum >> 1) & 0x1f) * 255 + 15) // 31
        a = (datum & 0x1) * 255
        yield r, g, b, a


del _register_color_decoder


def uncuddle_paletted_pixels(palette, data):
    if len(palette) <= 16:
        # Short palettes allow cramming two pixels into each byte
        return (
            idx
            for byte in data
            for idx in (byte >> 4, byte & 0x0f)
        )
    else:
        return data


def untile_pixels(raw_pixels, width, height, *, is_flim):
    """Unscramble pixels into plain old rows.

    The pixels are arranged in 8×8 tiles, and each tile is a third-
    iteration Z-order curve.

    Taken from: https://github.com/Zhorken/pokemon-x-y-icons/
    """

    # Images are stored padded to powers of two
    stored_width = 2 ** math.ceil(math.log(width) / math.log(2))
    stored_height = 2 ** math.ceil(math.log(height) / math.log(2))
    num_pixels = stored_width * stored_height
    tile_width = stored_width // 8
    tile_height = stored_height // 8

    pixels = [
        [None for x in range(width)]
        for y in range(height)
    ]

    for n, pixel in enumerate(raw_pixels):
        if n >= num_pixels:
            break

        # Find the coordinates of the top-left corner of the current tile.
        # n.b. The image is eight tiles wide, and each tile is 8×8 pixels.
        tile_num = n // 64
        if is_flim:
            # The FLIM format seems to pseudo-rotate the entire image to the
            # right, so tiles start in the bottom left and go up
            tile_y = (tile_height - 1 - (tile_num % tile_height)) * 8
            tile_x = tile_num // tile_height * 8
        else:
            # CLIM has the more conventional right-then-down order
            tile_y = tile_num // tile_width * 8
            tile_x = tile_num % tile_width * 8

        # Determine the pixel's coordinates within the tile
        # http://en.wikipedia.org/wiki/Z-order_curve#Coordinate_values
        within_tile = n % 64

        sub_x = (
            (within_tile & 0b000001) |
            (within_tile & 0b000100) >> 1 |
            (within_tile & 0b010000) >> 2
        )
        sub_y = (
            (within_tile & 0b000010) >> 1 |
            (within_tile & 0b001000) >> 2 |
            (within_tile & 0b100000) >> 3
        )

        if is_flim:
            # Individual tiles are also rotated.  Unrotate them
            sub_x, sub_y = sub_y, 7 - sub_x

        # Add up the pixel's coordinates within the whole image
        x = tile_x + sub_x
        y = tile_y + sub_y

        if x < width and y < height:
            pixels[y][x] = pixel

    return pixels


def decode_clim(data):
    file_format = data[-40:-36]
    if file_format == b'CLIM':
        is_flim = False
    elif file_format == b'FLIM':
        is_flim = True
    else:
        raise ValueError("Unknown image format {}".format(file_format))

    imag_header = imag_header_struct.parse(data[-20:])
    if is_flim:
        # TODO SUMO hack; not sure how to get format out of this header
        imag_header.format = 'RGBA5551'

    if imag_header.format not in COLOR_DECODERS:
        raise ValueError(
            "don't know how to decode {} pixels".format(imag_header.format))
    color_decoder, color_bpp, color_depth = COLOR_DECODERS[imag_header.format]

    mode, = struct.unpack_from('<H', data, 0)
    if mode == 2:
        # Paletted
        palette_length, = struct.unpack_from('<H', data, 2)
        palette = list(color_decoder(data, start=4, count=palette_length))
        data_start = 4 + palette_length * color_bpp
        scrambled_pixels = uncuddle_paletted_pixels(palette, data[data_start:])
    else:
        palette = None
        scrambled_pixels = color_decoder(data)

    pixels = untile_pixels(
        scrambled_pixels,
        imag_header.width,
        imag_header.height,
        is_flim=is_flim,
    )
    return imag_header.width, imag_header.height, color_depth, palette, pixels