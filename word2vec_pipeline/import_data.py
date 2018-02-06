"""
Import documents into the pipeline, concatenate target fields into a 
specifc field, and tag each document with a unique reference ID. 
Identifies common phrases found in the document.
"""

import os
import sys
import csv
import itertools
import collections

import nlpre
import pandas as pd

from utils.os_utils import mkdir, grab_files
from utils.parallel_utils import jobmap
import utils.db_utils as db_utils

from tqdm import tqdm

# Fix for pathological csv files
csv.field_size_limit(sys.maxsize)
_ref_counter = itertools.count()

parser_parenthetical = nlpre.identify_parenthetical_phrases()
parser_unicode = nlpre.unidecoder()


def func_parenthetical(data, **kwargs):
    '''
    Identify paranthetical phrases in the data

    Args:
        data: a text document
        kwargs: additional arguments
    Returns:
        parser_parenthetical(text): A collections.counter object with 
                                    count of parenthetical phrases
    '''
    text = data[kwargs["col"]]
    return parser_parenthetical(text)

def map_to_unicode(s):
    '''
    Convert input string to unicode.

    Args:
        s: an input string document

    Returns
        s: a copy of the input string in unicode
    '''
    # Helper function to fix input format
    s = str(s)
    return s.decode('utf-8', errors='replace')


def clean_row(row):
    '''
    Maps all keys through a unicode and unidecode fixer.

    Args:
        row: a row of text

    Returns:
        row: the same row of text converted to unicode
    '''
    for key, val in row.iteritems():
        row[key] = parser_unicode(map_to_unicode(val))
    return row

def csv_iterator(f_csv, clean=True, _PARALLEL=False):
    '''
    Creates an iterator over a CSV file, optionally cleans it.

    Args
        f_csv (str): Filename of the csv to open and iterate over
        clean (bool): Set whether to clean the csv file
        PARALLEL (bool): Set whether the iterator should be run in parallel
    '''
    
    with open(f_csv) as FIN:
        CSV = csv.DictReader(FIN)

        if clean and _PARALLEL:
            CSV = jobmap(clean_row, CSV, FLAG_PARALLEL=_PARALLEL)
        elif clean and not _PARALLEL:
            CSV = itertools.imap(clean_row, CSV)

        try:
            for row in CSV:
                yield row
        except Exception:
            pass

# Any reason it takes a list as an input, instead of the 4 parameters?
def import_csv(item):
    """
    Import a csv file, optionally merging select fields into a single field.

    Args:
        item: a list containing function paramters
            f_csv (str): Filename of the csv file to open
            f_csv_out (str): Filename of output csv
            f_target_column (str): Name of the column with concatenated text
            merge_columns (list): Names of the text columns that are 
              to be concatenated
    """
    (f_csv, f_csv_out, target_column, merge_columns) = item
    has_checked_keys = False

    if not merge_columns:
        raise ValueError("merge_columns must not be empty")

    with open(f_csv_out, 'w') as FOUT:
        CSV_HANDLE = None
        total_rows = 0

        for row in tqdm(csv_iterator(f_csv)):

            output = {"_ref":_ref_counter.next()}

            if not has_checked_keys:
                for key in merge_columns:
                    if key not in row.keys():
                        msg = "column {} not in csv file {}"
                        raise KeyError(msg.format(key, f_csv))
                has_checked_keys = True

            if target_column in row.keys():
                msg = "generated column {} already in csv file {}"
                raise KeyError(msg.format(target_column, f_csv))

            text = []
            for key in merge_columns:
                val = row[key].strip()
                if not val:
                    continue
                if val[-1] not in ".?!,":
                    val += '.'
                text.append(val)

            output[target_column] = '\n'.join(text).strip()            

            if CSV_HANDLE is None:
                CSV_HANDLE = csv.DictWriter(FOUT, sorted(output.keys()))
                CSV_HANDLE.writeheader()

            CSV_HANDLE.writerow(output)
            total_rows += 1

        print("Imported {}, {} entries".format(f_csv, total_rows))


def import_directory_csv(d_in, d_out, target_column, merge_columns):
    '''
    Takes a input_directory and output_directory and builds
    and cleaned (free of encoding errors) CSV for all input
    and attaches unique _ref numbers to each entry.

    Args:
        d_in (str): Directory of the csv file to open
        d_out (str): Directory of where the input document should be saved to
        target_column (str): Name of the column with concatenated text
        merge_columns (list): Names of the text columns that are to be 
           concatenated
    '''

    INPUT_FILES = grab_files("*.csv", d_in)

    if not INPUT_FILES:
        print("No matching CSV files found, exiting")
        exit(2)

    for f_csv in INPUT_FILES:
        f_csv_out = os.path.join(d_out, os.path.basename(f_csv))
        vals = (f_csv, f_csv_out, target_column, merge_columns)
        import_csv(vals)


def import_data_from_config(config):
    """
    Import parameters from the config file. import_data_from_config() 
    and phrases_from_config() are the entry points for this step of the 
    pipeline.

    Args:
        config: a config file
    """

    merge_columns = config["import_data"]["merge_columns"]

    if (not isinstance(merge_columns, list)):
        msg = "merge_columns (if used) must be a list"
        raise ValueError(msg)

    data_out = config["import_data"]["output_data_directory"]
    mkdir(data_out)

    # Require 'input_data_directories' to be a list
    data_in_list = config["import_data"]["input_data_directories"]
    if (not isinstance(data_in_list, list)):
        msg = "input_data_directories must be a list"
        raise ValueError(msg)

    target_column = config["target_column"]

    for d_in in data_in_list:
        import_directory_csv(d_in, data_out, target_column, merge_columns)


def dedupe_abbr(ABR):
    """
    Remove duplicate entries in dictionary of abbreviations

    Args:
        ABR: a dictionary of abbreviations and corresponding phrases

    Returns:
        df: a DataFrame of sorted abbreviations
    """

    df = pd.DataFrame()
    df['phrase'] = [' '.join(x[0]) for x in ABR.keys()]
    df['abbr'] = [x[1] for x in ABR.keys()]
    df['count'] = ABR.values()

    # Match phrases on lowercase and remove trailing 's'
    df['reduced_phrase'] = df.phrase.str.strip()
    df['reduced_phrase'] = df.reduced_phrase.str.lower()
    df['reduced_phrase'] = df.reduced_phrase.str.rstrip('s')

    data = []
    for phrase, dfx in df.groupby('reduced_phrase'):
        top = dfx.sort_values("count", ascending=False).iloc[0]

        item = {}
        item["count"] = dfx["count"].sum()
        item["phrase"] = top["phrase"]
        item["abbr"] = top["abbr"]
        data.append(item)

    df = pd.DataFrame(data).set_index("phrase")
    return df.sort_values("count", ascending=False)


def phrases_from_config(config):
    """
    Identify parenthetical phrases in the documents as they are being 
    imported to the pipeline.

    import_data_from_config() and phrases_from_config() are the entry 
    points for this step of the pipeline.

    Args:
        config: a config file
    :return:
    """

    _PARALLEL = config.as_bool("_PARALLEL")
    output_dir = config["phrase_identification"]["output_data_directory"]

    target_column = config["target_column"]

    import_config = config["import_data"]
    input_data_dir = import_config["output_data_directory"]

    F_CSV = grab_files("*.csv", input_data_dir)
    ABR = collections.Counter()

    dfunc = db_utils.CSV_database_iterator
    INPUT_ITR = dfunc(F_CSV, target_column, progress_bar=True)
    ITR = jobmap(func_parenthetical, INPUT_ITR, _PARALLEL, col=target_column)

    for result in ITR:
        ABR.update(result)

    msg = "\n{} total abbrs found."
    print(msg.format(len(ABR)))

    # Merge abbreviations that are similar
    print("Deduping abbr list.")
    df = dedupe_abbr(ABR)
    print("{} abbrs remain after deduping".format(len(df)))

    # Output top phrase
    print("Top 5 abbreviations")
    print(df[:5])

    mkdir(output_dir)
    f_csv = os.path.join(output_dir,
                         config["phrase_identification"]["f_abbreviations"])
    df.to_csv(f_csv)


if __name__ == "__main__":

    import simple_config
    config = simple_config.load()

    # import_data_from_config(config)
    phrases_from_config(config)
