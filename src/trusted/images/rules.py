import numpy as np
import pandas as pd

MIN_SIDE = 480            # px
MIN_CONTRAST = 5.0        # std de la lluminositat: sota això és gairebé uniforme
BLUR_THRESHOLD = 50.0     # variància del Laplacià
DARK_THRESHOLD = 30.0
BRIGHT_THRESHOLD = 225.0
LETTERBOX_THRESHOLD = 0.10
NEAR_DUP_DISTANCE = 5     # distància de Hamming entre pHash de 64 bits


def _hamming(value: np.uint64, others: np.ndarray) -> np.ndarray:
    xor = np.bitwise_xor(others, value)
    return np.unpackbits(xor.view(np.uint8).reshape(-1, 8), axis=1).sum(axis=1)


def flag_duplicates(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    valid = df["error"].isna()

    # Duplicats exactes (mateix contingut)
    df["dup_exact"] = valid & df.duplicated("sha256", keep="first")

    # Gairebé duplicats (mateix pHash aprox.): es conserva la primera aparició
    df["dup_near"] = False
    candidates = df[valid & ~df["dup_exact"]]
    index = candidates.index.to_numpy()
    hashes = np.array([int(h, 16) for h in candidates["phash"]], dtype=np.uint64)

    kept = np.empty(len(hashes), dtype=np.uint64)
    n_kept = 0
    for pos, h in enumerate(hashes):
        if n_kept and (_hamming(h, kept[:n_kept]) <= NEAR_DUP_DISTANCE).any():
            df.at[index[pos], "dup_near"] = True
        else:
            kept[n_kept] = h
            n_kept += 1
    return df


def apply_rules(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Flags suaus (només es registren)
    df["flag_blurry"] = df["sharpness"] < BLUR_THRESHOLD
    df["flag_dark"] = df["brightness"] < DARK_THRESHOLD
    df["flag_bright"] = df["brightness"] > BRIGHT_THRESHOLD
    df["flag_letterbox"] = df["letterbox_ratio"] > LETTERBOX_THRESHOLD

    # Regles dures (eliminen); la darrera assignació té prioritat
    df["reject_reason"] = None
    df.loc[df["dup_near"] | df["dup_exact"], "reject_reason"] = "duplicate"
    df.loc[df["contrast"] < MIN_CONTRAST, "reject_reason"] = "blank"
    df.loc[(df["width"] < MIN_SIDE) | (df["height"] < MIN_SIDE), "reject_reason"] = "too_small"
    df.loc[df["error"].notna(), "reject_reason"] = "corrupt"
    return df