"""Unit tests for the image formatting rules (any format -> RGB JPEG) and the Formatted step."""

import io

import pytest
from botocore.exceptions import ClientError
from PIL import Image, UnidentifiedImageError

# Adjust this import if your module has another name.
from src.formatting.images import persistent_to_formatted as fmt

LANDING = "landing"
FORMATTED = "formatted"
STORAGE = {
    "buckets": {"landing": LANDING, "formatted": FORMATTED},
    "landing": {"persistent_prefix": "persistent"},
}
SOURCE = "persistent/images/rawg$games$1$18-10-2026.png"
TARGET = "images/rawg$games$1$18-10-2026.jpg"


def decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


# ---- colour modes ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "fmt_name"),
    [("RGB", "PNG"), ("L", "PNG"), ("P", "PNG"), ("RGBA", "PNG"), ("LA", "PNG"),
     ("1", "PNG"), ("CMYK", "TIFF"), ("I;16", "PNG")],
)
def test_every_mode_ends_up_as_rgb(image_bytes, mode, fmt_name):
    with Image.open(io.BytesIO(image_bytes(mode=mode, fmt=fmt_name))) as image:
        image.load()
        assert fmt.to_rgb(image).mode == "RGB"


def test_transparent_pixels_become_white(image_bytes):
    raw = image_bytes(mode="RGBA", color=(0, 0, 0, 0))
    assert decode(fmt.convert_to_jpeg(raw)).getpixel((5, 5)) == (255, 255, 255)


def test_palette_transparency_becomes_white(image_bytes):
    raw = image_bytes(mode="P", color=0, transparency=0)
    assert decode(fmt.convert_to_jpeg(raw)).getpixel((5, 5)) == (255, 255, 255)


def test_opaque_pixels_keep_their_colour(image_bytes):
    raw = image_bytes(mode="RGBA", color=(200, 10, 10, 255))
    red, green, blue = decode(fmt.convert_to_jpeg(raw)).getpixel((5, 5))
    assert red > 190 and green < 25 and blue < 25


@pytest.mark.parametrize(("value", "expected"), [(0, 0), (32768, 128), (65535, 255)])
def test_16_bit_images_are_scaled_to_8_bits(image_bytes, value, expected):
    raw = image_bytes(mode="I;16", color=value)
    red, green, blue = decode(fmt.convert_to_jpeg(raw)).getpixel((5, 5))
    assert red == pytest.approx(expected, abs=3)
    assert green == pytest.approx(expected, abs=3)
    assert blue == pytest.approx(expected, abs=3)


def test_animated_gif_keeps_only_the_first_frame():
    red = Image.new("RGB", (16, 16), (255, 0, 0))
    blue = Image.new("RGB", (16, 16), (0, 0, 255))
    buffer = io.BytesIO()
    red.save(buffer, format="GIF", save_all=True, append_images=[blue], duration=100)
    red_value, _, blue_value = decode(fmt.convert_to_jpeg(buffer.getvalue())).getpixel((5, 5))
    assert red_value > 200 and blue_value < 60


def test_exif_orientation_is_applied_before_the_metadata_is_lost(image_bytes):
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees
    raw = image_bytes(fmt="JPEG", size=(40, 20), color=(10, 120, 200), exif=exif)
    assert decode(raw).size == (40, 20)
    assert decode(fmt.convert_to_jpeg(raw)).size == (20, 40)


def test_output_is_an_rgb_jpeg_with_the_same_size(image_bytes):
    result = decode(fmt.convert_to_jpeg(image_bytes(size=(70, 30), color=(1, 2, 3))))
    assert (result.format, result.mode, result.size) == ("JPEG", "RGB", (70, 30))


# ---- bad files --------------------------------------------------------------------------------------


def test_garbage_bytes_are_not_an_image():
    with pytest.raises(UnidentifiedImageError):
        fmt.convert_to_jpeg(b"definitely not an image")


def test_truncated_image_is_detected(image_bytes):
    raw = image_bytes(seed=1, size=(200, 200))
    with pytest.raises(OSError):
        fmt.convert_to_jpeg(raw[: len(raw) // 2])


def test_decompression_bomb_is_detected(image_bytes, monkeypatch):
    raw = image_bytes(size=(30, 30))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    with pytest.raises(Image.DecompressionBombError):
        fmt.convert_to_jpeg(raw)


@pytest.mark.parametrize("error", [UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError])
def test_all_decoding_failures_count_as_bad_images(error):
    assert issubclass(error, fmt.BAD_IMAGE_ERRORS)


# ---- Formatted step -------------------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch, s3):
    monkeypatch.setattr(fmt, "load_storage_config", lambda: STORAGE)
    monkeypatch.setattr(fmt, "create_minio_client", lambda: s3)
    return s3


def test_head_or_none_does_not_hide_real_errors(s3):
    s3.put(FORMATTED, "a.jpg", b"x")
    s3.failures[("head_object", "a.jpg")] = ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")
    with pytest.raises(ClientError):
        fmt.head_or_none(s3, FORMATTED, "a.jpg")
    assert fmt.head_or_none(s3, FORMATTED, "absent.jpg") is None


def test_image_is_converted_to_a_jpeg_named_after_the_stem(env, image_bytes):
    env.put(LANDING, SOURCE, image_bytes(size=(50, 40), color=(9, 99, 199)))
    assert fmt.format_images() == 0
    assert env.keys(FORMATTED) == [TARGET]
    assert env.content_type(FORMATTED, TARGET) == "image/jpeg"
    assert decode(env.data(FORMATTED, TARGET)).format == "JPEG"


def test_already_converted_images_are_skipped_without_reading_the_source(env, image_bytes):
    env.put(LANDING, SOURCE, image_bytes())
    env.put(FORMATTED, TARGET, b"previous result")
    assert fmt.format_images() == 0
    assert env.calls["get_object"] == 0
    assert env.data(FORMATTED, TARGET) == b"previous result"


def test_corrupt_image_goes_to_quarantine_and_is_not_an_error(env):
    env.put(LANDING, SOURCE, b"not an image")
    assert fmt.format_images() == 0
    assert env.keys(FORMATTED) == ["quarantine/images/rawg$games$1$18-10-2026.png"]
    assert env.data(FORMATTED, "quarantine/images/rawg$games$1$18-10-2026.png") == b"not an image"


def test_only_images_under_persistent_images_are_processed(env, image_bytes):
    env.put(LANDING, "persistent/images/notes.txt", b"text")
    env.put(LANDING, "persistent/audio/rawg$sound$1$18-10-2026.png", image_bytes())
    env.put(LANDING, "temporal/images/rawg$games$2$18-10-2026.png", image_bytes())
    assert fmt.format_images() == 0
    assert env.keys(FORMATTED) == []


def test_every_page_of_the_listing_is_processed(env, image_bytes):
    for index in range(5):
        env.put(LANDING, f"persistent/images/rawg$games${index}$18-10-2026.png", image_bytes(seed=index))
    assert fmt.format_images() == 0
    assert len(env.keys(FORMATTED, "images/")) == 5


def test_dry_run_writes_nothing_not_even_quarantine(env, image_bytes):
    env.put(LANDING, SOURCE, image_bytes())
    env.put(LANDING, "persistent/images/broken.png", b"not an image")
    assert fmt.format_images(dry_run=True) == 0
    assert env.keys(FORMATTED) == []


def test_a_read_error_is_reported_but_the_other_images_are_converted(env, image_bytes):
    env.put(LANDING, "persistent/images/a.png", image_bytes(seed=1))
    env.put(LANDING, "persistent/images/b.png", image_bytes(seed=2))
    env.failures[("get_object", "persistent/images/a.png")] = ConnectionError("network down")
    assert fmt.format_images() == 1
    assert env.keys(FORMATTED) == ["images/b.jpg"]


def test_a_write_error_makes_the_run_fail(env, image_bytes):
    env.put(LANDING, SOURCE, image_bytes())
    env.failures[("put_object", TARGET)] = ConnectionError("network down")
    assert fmt.format_images() == 1


def test_a_truncated_upload_is_detected(env, image_bytes, monkeypatch):
    env.put(LANDING, SOURCE, image_bytes())
    original = type(env).put_object
    monkeypatch.setattr(
        env, "put_object",
        lambda Bucket, Key, Body, ContentType=None: original(env, Bucket, Key, Body[:-1], ContentType),
    )
    assert fmt.format_images() == 1


def test_rerun_after_success_converts_nothing_new(env, image_bytes):
    env.put(LANDING, SOURCE, image_bytes())
    fmt.format_images()
    reads = env.calls["get_object"]
    assert fmt.format_images() == 0
    assert env.calls["get_object"] == reads


@pytest.mark.xfail(strict=True, reason=(
    "Known limitation: the formatted key is built from the stem only, so the same name in two "
    "formats (x.png and x.jpg) collides and the second one is skipped silently. Keep the original "
    "extension in the key (x.png.jpg), as the audio pipeline does, to fix it."))
def test_same_name_in_two_formats_must_not_collide(env, image_bytes):
    env.put(LANDING, "persistent/images/rawg$games$1$18-10-2026.png", image_bytes(seed=1))
    env.put(LANDING, "persistent/images/rawg$games$1$18-10-2026.jpg", image_bytes(seed=2, fmt="JPEG"))
    fmt.format_images()
    assert len(env.keys(FORMATTED, "images/")) == 2