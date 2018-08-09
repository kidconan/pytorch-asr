import sys
import argparse
from pathlib import Path
import subprocess as sp
import random

import numpy as np

from tqdm import tqdm
import torch

from ..utils.kaldi_io import smart_open, read_string, read_vec_int
from ..utils.logger import logger, set_logfile
from ..utils.misc import get_num_lines, remove_duplicates
from ..utils import params as p
from ..kaldi._path import KALDI_ROOT


"""
This recipe requires Kaldi's egs/aspire/s5 recipe directory containing the result
of its own scripts, especially the data/ and the exp/
"""

KALDI_PATH = Path(KALDI_ROOT).resolve()
RECIPE_PATH = Path(KALDI_PATH, "egs", "aspire", "ics").resolve()
DATA_PATH = Path(__file__).parents[2].joinpath("data", "aspire").resolve()

assert KALDI_PATH.exists(), f"no such path \"{str(KALDI_PATH)}\" not found"
assert RECIPE_PATH.exists(), f"no such path \"{str(RECIPE_PATH)}\" not found"

WIN_SAMP_SIZE = p.SAMPLE_RATE * p.WINDOW_SIZE
WIN_SAMP_SHIFT = p.SAMPLE_RATE * p.WINDOW_SHIFT
#SAMPLE_MARGIN = WIN_SAMP_SHIFT * p.FRAME_MARGIN  # samples
SAMPLE_MARGIN = 0


CHAR_MASK = "abcdefghijklmnopqrstuvwxyz'-._<>[] "


def strip_text(text):
    text = text.lower()
    text = ''.join([x for x in text if x in CHAR_MASK])
    return text


def split_wav(mode, target_dir):
    import io
    import wave

    data_dir = Path(RECIPE_PATH, "data", mode).resolve()
    segments_file = Path(data_dir, "segments")
    logger.info(f"processing {str(segments_file)} file ...")
    segments = dict()
    with smart_open(segments_file, "r") as f:
        for line in tqdm(f, total=get_num_lines(segments_file)):
            split = line.strip().split()
            uttid, wavid, start, end = split[0], split[1], float(split[2]), float(split[3])
            if wavid in segments:
                segments[wavid].append((uttid, start, end))
            else:
                segments[wavid] = [(uttid, start, end)]

    wav_scp = Path(data_dir, "wav.scp")
    logger.info(f"processing {str(wav_scp)} file ...")
    manifest = dict()
    with smart_open(wav_scp, "r") as rf:
        for line in tqdm(rf, total=get_num_lines(wav_scp)):
            wavid, cmd = line.strip().split(" ", 1)
            if not wavid in segments:
                continue
            cmd = cmd.strip().rstrip(' |').split()
            p = sp.run(cmd, stdout=sp.PIPE, stderr=sp.PIPE)
            fp = io.BytesIO(p.stdout)
            with wave.openfp(fp, "rb") as wav:
                fr = wav.getframerate()
                nf = wav.getnframes()
                for uttid, start, end in segments[wavid]:
                    fs, fe = int(fr * start - SAMPLE_MARGIN), int(fr * end + SAMPLE_MARGIN)
                    if fs < 0 or fe > nf:
                        continue
                    wav.rewind()
                    wav.setpos(fs)
                    signal = wav.readframes(fe - fs)
                    p = uttid.find('-')
                    if p != -1:
                        tar_path = Path(target_dir, mode).joinpath(uttid[:p])
                    else:
                        tar_path = Path(target_dir, mode)
                    tar_path.mkdir(mode=0o755, parents=True, exist_ok=True)
                    wav_file = tar_path.joinpath(uttid + ".wav")
                    with wave.open(str(wav_file), "wb") as wf:
                        wf.setparams(wav.getparams())
                        wf.writeframes(signal)
                    manifest[uttid] = (str(wav_file), fe - fs)
    return manifest


def get_transcripts(mode, target_dir):
    data_dir = Path(RECIPE_PATH, "data", mode).resolve()
    texts_file = Path(data_dir, "text")
    logger.info(f"processing {str(texts_file)} file ...")
    manifest = dict()
    with smart_open(Path(data_dir, "text"), "r") as f:
        with open(Path(target_dir, f"{mode}_convert.txt"), "w") as wf:
            for line in tqdm(f, total=get_num_lines(texts_file)):
                try:
                    uttid, text = line.strip().split(" ", 1)
                    managed_text = strip_text(text)
                    wf.write(f"{uttid}: {text}\n")
                    wf.write(f"{uttid}: {managed_text}\n")
                    if len(managed_text) == 0:
                        continue
                except:
                    continue
                p = uttid.find('-')
                if p != -1:
                    tar_path = Path(target_dir, mode).joinpath(uttid[:p])
                else:
                    tar_path = Path(target_dir, mode)
                tar_path.mkdir(mode=0o755, parents=True, exist_ok=True)
                txt_file = tar_path.joinpath(uttid + ".txt")
                with open(str(txt_file), "w") as txt:
                    txt.write(managed_text + "\n")
                manifest[uttid] = (str(txt_file), managed_text)
    return manifest


def get_alignments(target_dir):
    import io
    import pipes
    import gzip

    exp_dir = Path(RECIPE_PATH, "exp", "tri5a").resolve()
    models = exp_dir.glob("*.mdl")
    model = sorted(models, key=lambda x: x.stat().st_mtime)[-1]

    logger.info("processing alignment files ...")
    logger.info(f"using the trained kaldi model: {model}")
    manifest = dict()
    alis = [x for x in exp_dir.glob("ali.*.gz")]
    for ali in tqdm(alis):
        cmd = [ str(Path(KALDI_PATH, "src", "bin", "ali-to-phones")),
                "--per-frame", f"{model}", f"ark:-", f"ark,f:-" ]
        with gzip.GzipFile(ali, "rb") as a:
            p = sp.run(cmd, stdout=sp.PIPE, stderr=sp.PIPE, input=a.read())
            with io.BytesIO(p.stdout) as f:
                while True:
                    # mkdir
                    try:
                        uttid = read_string(f)
                    except ValueError:
                        break
                    tar_dir = Path(target_dir).joinpath(uttid[6:9])
                    tar_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
                    # store phn file
                    phn_file = tar_dir.joinpath(uttid + ".phn")
                    phones = read_vec_int(f)
                    np.savetxt(str(phn_file), phones, "%d")
                    # prepare manifest elements
                    num_frms = len(phones)
                    manifest[uttid] = (str(phn_file), num_frms)
    return manifest


def make_ctc_labels(target_dir):
    # find *.phn files
    logger.info(f"finding *.phn files under {target_dir}")
    phn_files = [str(x) for x in Path(target_dir).rglob("*.phn")]
    # convert
    for phn_file in tqdm(phn_files):
        phns = np.loadtxt(phn_file, dtype="int", ndmin=1)
        # make ctc labelings by removing duplications
        ctcs = np.array([x for x in remove_duplicates(phns)])
        # write ctc file
        # blank labels will be inserted in warp-ctc loss module,
        # so here the target labels have not to contain the blanks interleaved
        ctc_file = phn_file.replace("phn", "ctc")
        np.savetxt(str(ctc_file), ctcs, "%d")
    count_priors(phn_files)


def count_priors(target_dir, phn_files=None):
    # load labels.txt
    labels = dict()
    with open('asr/kaldi/graph/labels.txt', 'r') as f:
        for line in f:
            splits = line.strip().split()
            label = splits[0]
            labels[label] = splits[1]
    blank = labels['<blk>']
    if phn_files is None:
        # find *.phn files
        logger.info(f"finding *.phn files under {target_dir}")
        phn_files = [str(x) for x in Path(target_dir).rglob("*.phn")]
    # count
    counts = [0] * len(labels)
    for phn_file in tqdm(phn_files):
        phns = np.loadtxt(phn_file, dtype="int", ndmin=1)
        # count labels for priors
        for c in phns:
            counts[int(c)] += 1
        counts[int(blank)] += len(phns) + 1
    # write count file
    count_file = Path(target_dir).joinpath("priors_count.txt")
    np.savetxt(str(count_file), counts, "%d")


def make_manifest(mode, target_path, rebuild=False):
    logger.info(f"processing \"{mode}\" ...")
    if rebuild:
        import wave
        wav_manifest, txt_manifest = dict(), dict()
        for wav_file in target_path.joinpath(mode).rglob("*.wav"):
            uttid = wav_file.stem
            with wave.openfp(str(wav_file), "rb") as wav:
                samples = wav.getnframes()
            wav_manifest[uttid] = (str(wav_file), samples)
            txt_file = str(wav_file).replace('wav', 'txt')
            txt_manifest[uttid] = (str(txt_file), '-')
        logger.info(f"rebuilding manifest to \"{mode}.csv\" ...")
    else:
        wav_manifest = split_wav(mode, target_path)
        txt_manifest = get_transcripts(mode, target_path)
        logger.info(f"generating manifest to \"{mode}.csv\" ...")

    min_len, max_len = 1e30, 0
    histo = [0] * 31
    total = 0
    with open(Path(target_path, f"{mode}.csv"), "w") as f:
        for k, v in tqdm(wav_manifest.items()):
            if not k in txt_manifest:
                continue
            wav_file, samples = v
            txt_file, _ = txt_manifest[k]
            f.write(f"{k},{wav_file},{samples},{txt_file}\n")
            total += 1
            sec = float(samples) / p.SAMPLE_RATE
            if sec < min_len:
                min_len = sec
            if sec > max_len:
                max_len = sec
            if sec < 30.:
                histo[int(np.ceil(sec))] += 1
    logger.info(f"total {total} entries listed in the manifest file.")
    cum_histo = np.cumsum(histo) / total * 100.
    logger.info(f"min: {min_len:.2f} sec  max: {max_len:.2f} sec")
    logger.info(f"<5 secs: {cum_histo[5]:.2f} %  "
                f"<10 secs: {cum_histo[10]:.2f} %  "
                f"<15 secs: {cum_histo[15]:.2f} %  "
                f"<20 secs: {cum_histo[20]:.2f} %  "
                f"<25 secs: {cum_histo[25]:.2f} %  "
                f"<30 secs: {cum_histo[30]:.2f} %")


def process(target_dir=None, rebuild=False):
    if target_dir is None:
        target_path = DATA_PATH
    else:
        target_path = Path(target_dir).resolve()
    logger.info(f"target data path : {target_path}")

    make_manifest("train", target_path, rebuild)
    make_manifest("dev", target_path, rebuild)
    make_manifest("test", target_path, rebuild)

    logger.info("data preparation finished.")


def prepare(argv):
    parser = argparse.ArgumentParser(description="Prepare dataset by importing from Kaldi recipe")
    parser.add_argument('--manifest-only', default=False, action='store_true', help="if you want to rebuild manifest only instead of the overall processing")
    parser.add_argument('path', type=str, help="path to store the processed data")
    args = parser.parse_args(argv)

    assert args.path is not None

    log_file = Path(args.path, 'prepare.log').resolve()
    print(f"begins logging to file: {str(log_file)}")
    set_logfile(log_file)

    process(args.path, rebuild=args.manifest_only)


if __name__ == "__main__":
    pass
