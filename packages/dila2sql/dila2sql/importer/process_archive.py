import os
import json
from collections import defaultdict
import concurrent.futures
import libarchive
from .utils import get_table, get_dossier
from ..utils import consume
from .suppress import suppress
from .process_xml import process_xml
from dila2sql.utils import connect_db, progressbar
from dila2sql.models import db_proxy, DBMeta

PROCESS_XML_JOBS_BATCH_SIZE = 1000
MAX_PROCESSES = int(os.getenv("DILA2SQL_MAX_PROCESSES")) if os.getenv("DILA2SQL_MAX_PROCESSES") else None
CHUNK_SIZE = 500_000


def process_archive(db, db_url, archive_path, process_links=True):
    base = DBMeta.get(DBMeta.key == 'base').value or 'LEGI'
    unknown_folders = defaultdict(lambda: 0)
    liste_suppression = []
    counts, skipped = (defaultdict(zero), 0)

    print("counting entries in archive ...")
    with libarchive.file_reader(archive_path) as archive:
        total = sum(1 for _ in archive)
    print(f"counted {total} entries in archive.")

    chunks_count, last_chunk_size = divmod(total, CHUNK_SIZE)
    chunks = [[i * CHUNK_SIZE, (i + 1) * CHUNK_SIZE] for i in range(chunks_count)]
    last_idx = chunks[-1][-1]
    chunks += [[last_idx, last_idx + last_chunk_size]] if last_chunk_size > 0 else []
    if len(chunks) > 1:
        print(f"big archive will be processed in {len(chunks)} chunks...")
    for chunk_idx, chunk in enumerate(chunks):
        chunk_start_idx, chunk_end_idx = chunk
        chunk_size = chunk_end_idx - chunk_start_idx
        entries = iterate_archive_chunk_entries(archive_path, chunk_start_idx, chunk_end_idx)
        print(f"chunk {chunk_idx}: generating XML jobs args for {chunk_size} entries starting from idx {chunk_start_idx}")
        process_xml_jobs_args = [
            get_process_xml_job_args_for_entry(
                entry,
                base, counts, skipped, liste_suppression, unknown_folders,
                db, db_url, process_links=process_links
            ) for entry in entries
        ]
        print(f"chunk {chunk_idx}: start processing XML jobs...")
        process_xml_jobs_args = [a for a in process_xml_jobs_args if a is not None]
        if MAX_PROCESSES != 1 and len(process_xml_jobs_args) > 10 * PROCESS_XML_JOBS_BATCH_SIZE:
            chunk_counts, chunk_skipped = process_xml_jobs_in_parallel(process_xml_jobs_args, db_url)
        else:
            chunk_counts, chunk_skipped = process_xml_jobs_sync(process_xml_jobs_args, db=db, commit=True, progress=True)
        merge_counts(chunk_counts, chunk_skipped, counts, skipped)

    if liste_suppression:
        db = connect_db(db_url)
        db_proxy.initialize(db)
        suppress(base, db, liste_suppression)

    print(
        "made %s changes in the database:" % sum(counts.values()),
        json.dumps(counts, indent=4, sort_keys=True)
    )

    if skipped:
        print("skipped", skipped, "files that haven't changed")

    if unknown_folders:
        for d, x in unknown_folders.items():
            print("skipped", x, "files in unknown folder `%s`" % d)


def iterate_archive_chunk_entries(archive_path, chunk_start_idx, chunk_end_idx):
    with libarchive.file_reader(archive_path) as archive:
        consume(archive, chunk_start_idx)  # skips first n
        progressbar_iterator = progressbar(archive, total=chunk_end_idx - chunk_start_idx)
        idx = chunk_start_idx
        for entry in progressbar_iterator:
            if idx > chunk_end_idx:
                progressbar_iterator.refresh()
                break
            idx += 1
            yield entry


def get_process_xml_job_args_for_entry(
    entry,
    base, counts, skipped, liste_suppression, unknown_folders,
    db, db_url, process_links=True
):
    path = entry.pathname
    parts = path.split('/')
    if path[-1] == '/':
        return
    if parts[-1] == 'liste_suppression_'+base.lower()+'.dat':
        liste_suppression += b''.join(entry.get_blocks()).decode('ascii').split()
        return
    if parts[1] == base.lower():
        path = path[len(parts[0])+1:]
        parts = parts[1:]
    if (
        parts[0] not in ['legi', 'jorf', 'kali'] or
        (parts[0] == 'legi' and not parts[2].startswith('code_et_TNC_')) or
        (parts[0] == 'jorf' and parts[2] not in ['article', 'section_ta', 'texte']) or
        (parts[0] == 'kali' and parts[2] not in ['article', 'section_ta', 'texte', 'conteneur'])
    ):
        # https://github.com/Legilibre/legi.py/issues/23
        unknown_folders[parts[2]] += 1
        return
    table = get_table(parts)
    dossier = get_dossier(parts, base)
    text_cid = parts[11] if base == 'LEGI' else None
    text_id = parts[-1][:-4]
    if table is None:
        unknown_folders[text_id] += 1
        return
    xml_blob = b''.join(entry.get_blocks())
    mtime = entry.mtime
    return (xml_blob, mtime, base, table, dossier, text_cid, text_id, process_links)


def merge_counts(sub_counts, sub_skipped, counts, skipped):
    skipped += sub_skipped
    for key, count in sub_counts.items():
        counts[key] += count


def zero():
    return 0  # cannot use a lambda because not picklable for multiprocessing


def process_xml_jobs_batch(jobs_args_batch, db_url):
    # this will be ran in a separate process, thus we need to init our own DB connection
    db = connect_db(db_url)
    db_proxy.initialize(db)
    batch_counts, batch_skipped = process_xml_jobs_sync(jobs_args_batch, db=db, commit=False)
    db.commit()  # limits commits to db
    return batch_counts, batch_skipped


def process_xml_jobs_in_parallel(process_xml_jobs_args, db_url):
    print("starting process_xml tasks in a Process Pool...")
    progress_bar = progressbar(process_xml_jobs_args)
    counts, skipped = (defaultdict(zero), 0)
    batches = [
        process_xml_jobs_args[i:i + PROCESS_XML_JOBS_BATCH_SIZE]
        for i in range(0, len(process_xml_jobs_args), PROCESS_XML_JOBS_BATCH_SIZE)
    ]
    with concurrent.futures.ProcessPoolExecutor(max_workers=MAX_PROCESSES) as executor:
        futures = [executor.submit(process_xml_jobs_batch, batch, db_url) for batch in batches]
        for future in concurrent.futures.as_completed(futures):
            batch_counts, batch_skipped = future.result()
            merge_counts(batch_counts, batch_skipped, counts, skipped)
            progress_bar.update(PROCESS_XML_JOBS_BATCH_SIZE)
        progress_bar.close()
    return counts, skipped


def process_xml_jobs_sync(jobs_args, progress=False, db=None, commit=True):
    counts, skipped = (defaultdict(zero), 0)
    wrapped_jobs_args = progressbar(jobs_args) if progress else jobs_args
    for arg_list in wrapped_jobs_args:
        xml_counts, xml_skipped = process_xml(*arg_list)
        merge_counts(xml_counts, xml_skipped, counts, skipped)
        if commit:
            db.commit()
    return counts, skipped
