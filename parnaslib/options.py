# -*- coding: utf-8 -*-
import argparse
import os
import re
import subprocess
from argparse import RawTextHelpFormatter
from math import floor

from Bio import AlignIO
from Bio.Align import MultipleSeqAlignment
from dendropy import Tree

from parnaslib import parnas_logger

# Program interface:
parser = argparse.ArgumentParser(description='Phylogenetic mAximum RepreseNtAtion Sampling (PARNAS)',
                                 formatter_class=RawTextHelpFormatter)
parser.add_argument('-t', '--tree', type=str, action='store', dest='tree',
                    help='path to the input tree in newick or nexus format', required=True)
parser.add_argument('-n', type=int, action='store', dest='samples',
                    help='number of samples (representatives) to be chosen.\n' +
                         'This argument is required unless the --cover option is specified', required=True)
parser.add_argument('--color', type=str, action='store', dest='out_path',
                    help='PARNAS will save a colored tree, where the chosen representatives are highlighted '
                    'and the tree is color-partitioned respective to the representatives.\n'
                    'If prior centers are specified, they (and the subtrees they represent) will be colored red.')
parser.add_argument('--prior-regex', type=str, action='store', dest='prior_regex',
                    help='indicate the previous centers (if any) with a regex. '
                         'The regex should match a full taxon name.\n'
                         'PARNAS will then select centers that represent diversity '
                         'not covered by the previous centers.', required=False)
parser.add_argument('--threshold', type=float, action='store', dest='percent',
                    help='sequences similarity threshold: the algorithm will choose best representatives that cover as much\n' +
                         'diversity as possible within the given similarity threshold. ' +
                         '--nt or --aa must be specified with this option', required=False)
parser.add_argument('--cover', action='store_true',
                    help="choose the best representatives (smallest number) that cover all the tips within the specified threshold.\n" +
                    "If specified, the --threshold argument must be specified as well",
                    required=False)

taxa_handler = parser.add_argument_group('Excluding taxa')
taxa_handler.add_argument('--exclude', type=str, action='store', dest='exclude_regex',
                          help='Prohibits parnas to choose reoresentatives from the taxa matching this regex. '
                               'However, the excluded taxa will still contribute to the objective function.')
taxa_handler.add_argument('--exclude-fully', type=str, action='store', dest='full_regex',
                          help='Completely ignore the taxa matching this regex.')

alignment_parser = parser.add_argument_group('Sequence alignment')
alignment_parser.add_argument('--nt', type=str, action='store', dest='nt_alignment',
                    help='path to nucleotide sequences associated with the tree tips', required=False)
alignment_parser.add_argument('--aa', type=str, action='store', dest='aa_alignment',
                    help='path to amino acid sequences associated with the tree tips', required=False)
# parser.add_argument('--prior', metavar='TAXON', type=str, nargs='+',
#                     help='space-separated list of taxa that have been previously chosen as centers.\n' +
#                          'The algorithm will choose new representatives that cover the "new" diversity in the tree')


# Computes the coverage radius (# of substitutions) that satisfies the similarity threshold.
def threshold_to_substitutions(sim_threshold: float, alignment: MultipleSeqAlignment) -> int:
    subs = floor((1 - sim_threshold / 100) * len(alignment[0]))
    parnas_logger.info("%.3f%% similarity threshold implies that a single representative will cover all tips "
                       "in the %d-substitution radius." % (sim_threshold, subs))
    return subs


def reweigh_tree_ancestral(tree_path: str, alignment_path: str, aa=False) -> Tree:
    """
    Re-weighs the tree edges according to the number of substitutions per edge.
    The ancestral substitutions are inferred using TreeTime (Sargulenko et al. 2018).
    :param tree_path: path to the tree to be re-weighed.
    :param alignment_path: path to MSA associated with the tree tips.
    :param aa: whether the sequences consist of amino acid residues (default: nucleotide).
    :return: The re-weighed tree
    """
    # Run ancestral inference with treetime.
    treetime_outdir = 'treetime_ancestral_%s' % tree_path
    if not os.path.exists(treetime_outdir):
        os.mkdir(treetime_outdir)
    treetime_log_path = '%s/treetime.log' % treetime_outdir
    parnas_logger.info('Inferring ancestral substitutions with TreeTime. The log will be written to "%s".' % treetime_log_path)
    command = ['treetime', 'ancestral', '--aln', alignment_path, '--tree', tree_path, '--outdir', treetime_outdir,
               '--gtr', 'infer']
    if aa:
        command += ['--aa']
    treetime_out = '%s/annotated_tree.nexus' % treetime_outdir
    with open(treetime_log_path, 'w') as treetime_log:
        subprocess.call(command, stdout=treetime_log, stderr=subprocess.STDOUT)

    # Read the treetime output and weight the edges according to the number of subs.
    try:
        # Update the treetime output file.
        treetime_for_dendropy = '%s/ancestral_updated.nexus' % treetime_outdir
        with open(treetime_out, 'r') as treetime_nexus:
            with open(treetime_for_dendropy, 'w') as dendropy_nexus:
                for line in treetime_nexus:
                    for mutations in re.findall(r'mutations=".*?"', line):
                        upd_mutations = mutations.replace(',',
                                                          '||')  # DendroPy does not like commas in the annotations.
                        line = line.replace(mutations, upd_mutations)
                    dendropy_nexus.write(line)
        ancestral_tree = Tree.get(path=treetime_for_dendropy, schema='nexus', preserve_underscores=True)
    except Exception:
        parser.error('Failed to infer an ancestral tree with TreeTime. '
                     'Please see the TreeTime output log and consider inferring the ancestral states manually.')

    parnas_logger.info('Re-weighing the tree based on ancestral substitutions.')
    reweighed_tree = ancestral_tree
    for node in reweighed_tree.nodes():
        edge_length = 0
        mutations_str = node.annotations.get_value('mutations')
        if mutations_str and mutations_str.strip():
            edge_length = mutations_str.count('||') + 1
        node.edge_length = edge_length
    return reweighed_tree


def find_matching_taxa(tree: Tree, regex: str, title: str, none_message: str, print_taxa=True):
    matching_taxa = []
    for taxon in tree.taxon_namespace:
        if re.match(regex, taxon.label):
            matching_taxa.append(taxon.label)

    if print_taxa:
        if matching_taxa:
            parnas_logger.info(title)
            for t in matching_taxa:
                parnas_logger.plain('\t%s' % t)
        else:
            parnas_logger.info(none_message)
        parnas_logger.plain('')
    return matching_taxa


def parse_and_validate():
    args = parser.parse_args()

    # Validate the tree.
    tree = None
    try:
        tree = Tree.get(path=args.tree, schema='newick', preserve_underscores=True)
    except Exception:
        try:
            tree = Tree.get(path=args.tree, schema='nexus', preserve_underscores=True)
        except Exception:
            parser.error('Cannot read the specified tree file "%s". ' % args.tree +
                         'Make sure the tree is in the newick or nexus format.')

    # Validate n.
    n = args.samples
    if n < 1 or n >= len(tree.taxon_namespace):
        parser.error('n should be at least 1 and smaller than the number of taxa in the tree.')

    # Handle --prior-regex.
    prior_centers = None
    if args.prior_regex:
        prior_centers = find_matching_taxa(tree, args.prior_regex, 'Prior centers that match the regex:',
                                           'No taxa matched PRIOR_REGEX', True)

    # Validate exclusions.
    excluded_taxa = []
    fully_excluded = []
    if args.exclude_regex:
        excluded_taxa = find_matching_taxa(tree, args.exclude_regex,
                                           'Not considering the following as representatives (matched EXCLUDE_REGEX):',
                                           'No taxa matched EXCLUDE_REGEX', True)
    if args.full_regex:
        fully_excluded = find_matching_taxa(tree, args.full_regex, 'Ignoring the following taxa (matched FULL_REGEX):',
                                            'No taxa matched FULL_REGEX', True)

    exclude_intersection = set(excluded_taxa).intersection(set(fully_excluded))
    if exclude_intersection:
        for taxon in exclude_intersection:
            parnas_logger.warning(f'{taxon} matches both EXCLUDE_REGEX and FULL_REGEX. PARNAS will fully exclude it.')

    # Validate alignment.
    if args.nt_alignment or args.aa_alignment:
        if args.nt_alignment and args.aa_alignment:
            parser.error('Please specify EITHER the nucleotide or amino acid alignment - not both.')
        alignment_path = args.nt_alignment if args.nt_alignment else args.aa_alignment
        is_aa = args.aa_alignment is not None
        try:
            alignment = list(AlignIO.read(alignment_path, 'fasta'))
        except Exception:
            parser.error('Cannot read the specified FASTA alignment in "%s".' % alignment_path)
        alignment_present = True
    else:
        alignment_present = False

    # Validate threshold-related parameters and re-weigh the tree
    if args.percent:
        if args.percent <= 0 or args.percent >= 100:
            parser.error('Invalid "--threshold %.3f" option. The threshold must be between 0 and 100 (exclusive)'
                         % args.percent)
        if not alignment_present:
            parser.error('To use the --threshold parameter, please specify a nucleotide' +
                         'or amino acid alignment associated with the tree tips.')
        else:
            radius = threshold_to_substitutions(args.percent, alignment)
            query_tree = reweigh_tree_ancestral(args.tree, alignment_path, is_aa)
    else:
        query_tree = tree
        radius = None

    # Validate cover
    if args.cover:
        if not args.percent:
            parser.error('To use --cover parameter, please specify --threshold option.')

    return args, query_tree, n, radius, prior_centers, excluded_taxa, fully_excluded
