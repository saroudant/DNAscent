#!/usr/bin/env python

#----------------------------------------------------------
# Copyright 2017 University of Oxford
# Written by Michael A. Boemo (michael.boemo@path.ox.ac.uk)
#----------------------------------------------------------

import numpy as np
import warnings
import h5py
import pysam
import re
import os
import gc
import math
from joblib import Parallel, delayed #for parallel processing
import multiprocessing #for parallel processing
from utility import reverseComplement


def import_reference(filename):
#	takes the filename of a fasta reference sequence and returns the reference sequence as a string.  N.B. the reference file must have only one sequence in it
#	ARGUMENTS
#       ---------
#	- filename: path to a reference fasta file
#	  type: string
#	OUTPUTS
#       -------
#	- reference: reference string
#	  type: string

	f = open(filename,'r')
	g = f.readlines()
	f.close()

	reference = ''
	for line in g:
		if line[0] != '>':
			reference += line.rstrip()
	g = None

	reference = reference.upper()

	if not all(c in ['A','T','G','C','N'] for c in reference):
		warnings.warn('Warning: Illegal character in reference.  Legal characters are A, T, G, C, and N.', Warning)

	return reference


def export_reference(reference, header, filename):
#	takes a reference from import_reference and writes it to a fasta file, formatting it in all capitals and with 60 characters on each line
#	ARGUMENTS
#       ---------
#	- reference: reference string from import_reference
#	  type: string
#	- header: fasta header
#	  type: string
#	- filename: output filename
#	  type: string

	#write the fasta header at the top of the file
	f = open(filename,'w')
	f.write('>'+header+'\n')

	#divide the reference string into blocks of 60 characters
	referenceBlocks = [reference[i:i+60] for i in range(0, len(reference), 60)]

	for block in referenceBlocks:
		f.write(block+'\n')

	f.close()
	

def import_poreModel(filename):
#	takes the filename of an ONT pore model file and returns a map from kmer (string) to [mean,std] (list of floats)
#	ARGUMENTS
#       ---------
#	- filename: path to an ONT model file
#	  type: string
#	OUTPUTS
#       -------
#	- kmer2MeanStd: a map, keyed by a kmer, that returns the model mean and standard deviation signal for that kmer
#	  type: dictionary

	f = open(filename,'r')
	g = f.readlines()
	f.close()

	kmer2MeanStd = {}
	for line in g:
		if line[0] != '#' and line[0:4] != 'kmer': #ignore the header
			splitLine = line.split('\t')
			kmer2MeanStd[ splitLine[0] ] = [ float(splitLine[1]), float(splitLine[2]) ]
	g = None

	return kmer2MeanStd


def import_2Dfasta(pathToReads,outFastaFilename):
#	takes a directory with fast5 nanopore reads at the top level, and extracts the 2D sequences in fasta format with the path to the file as the fasta header
#	ARGUMENTS
#       ---------
#	- pathToReads: full path to the directory that contains the fast5 files
#	  type: string
#	- outFastaFilename: filename for the output fasta file that contains all of the reads
#	  type: string
#	OUTPUTS
#       -------
#	- a fasta file written to the directory specified

	buffersize = 1024

	#output file to write on
	fout = open(outFastaFilename,'w')

	#path through the fast5 tree to get to the fastq sequence
	fast5path2fastq = '/Analyses/Basecall_2D_000/BaseCalled_2D/Fastq'

	#empty reads string, and count the number of subdirectories so we can print progress
	reads = ''
	numSubdirectories = len(next(os.walk(pathToReads, topdown=True))[1])
	readCount = 0

	#recursively go through the directory and subdirectories and extract fasta seqs until you reach the buffer, then write, release, and garbage collect
	for root, dirs, files in os.walk(pathToReads, topdown=True):

		for fast5file in files:

			readCount += 1

			if fast5file.endswith('.fast5'):
		
				#print progress every 5 subdirectories of reads
				if readCount % 10000 == 0:
					print 'Exporting fast5 reads to fasta... read ' + str(readCount)

				try:
					#open the fast5 file with h5py and grab the fastq
					ffast5 = h5py.File(root+'/'+fast5file,'r')
					fastq = ffast5[fast5path2fastq].value
					ffast5.close()
					fasta = fastq.split('\n')[1]
			
					#append the sequence in the fasta format, with the full path to the fast5 file as the sequence name
					reads += '>'+root+'/'+fast5file+'\n'+fasta+'\n'

				except KeyError:
					warnings.warn('File '+root+'/'+fast5file+' did not have a valid fastq path.  Skipping.', Warning)

				except IOError:
					warnings.warn('File '+root+'/'+fast5file+' could not be opened and may be corrupted.  Skipping.', Warning)

				#write to the file and release the buffer
				if readCount % buffersize == 0:
					fout.write(reads)
					fout.flush()
					os.fsync(fout .fileno())
					reads = ''
					gc.collect()

		#flush the buffer and write once we're reached the end of fast5 files in the subdirectory
		fout.write(reads)
		fout.flush()
		os.fsync(fout .fileno())
		reads = ''
		gc.collect()
	
	#close output fasta file	
	fout.close()


def export_poreModel(emissions, outputFilename):
#	takes a dictionary of emissions produced by trainForAnalogue in train.py and outputs an ONT-style pore model file
#	ARGUMENTS
#       ---------
#	- emissions: keyed by a kmer string, outputs a list with kmer mean and standard deviation (generated by trainForAnalogue)
#	  type: dictionary
#	- outputFilename: path to the model file that should be created
#	  type: string
#	OUTPUTS
#       -------
#	- a model file is written to the directory specified

	#open the model file to write on
	f = open(outputFilename, 'w')

	#create header
	f.write('#model_name\ttemplate_median68pA.model.baseAnalogue\n')
	f.write('#type\tbase\n')
	f.write('#strand\ttemplate\n')
	f.write('#kit\tSQK007\n')
	f.write('kmer\tlevel_mean\tlevel_stdv\tsd_mean\tsd_stdv\n')

	#write kmer entries
	for key in emissions:
		toWrite = key+'\t'+str(emissions[key][0])+'\t'+str(emissions[key][1])+'\t0.0\t0.0\n'
		f.write(toWrite)

	#close the model file
	f.close()


def parallel_calculate_normalisedEvents(kmer, fast5Files, poreModelFile, progress):
#	For hairpin training data - the large number of kmers makes it best to use parallel processing.  Uses a 5mer model to
#	calculate the shift and scale pore-specific parameters for each individual read in a list of fast5 files
#	ARGUMENTS
#       ---------
#	- kmer: redundant 6mer to identify the reads we're normalising
#	  type: string
#	- fast5Files: list of fast5 files whose events should be normalised
#	  type: list of strings
#	- poreModelFile: path to a pore model file.  This should be a 5mer model in the ONT format
#	  type: string
#	- progress: shows the kmer we're on to give an idea of progress
#	  type: tuple of length two
#	OUTPUTS
#       -------
#	- allNormalisedReads: a list, where each member is itself a list of events that have been normalised to the pore model
#	  type: list

	#open the 5mer model and make a map from the 5mer (string) to [mean,std] (list)
	kmer2MeanStd = import_poreModel(poreModelFile)

	#now iterate through all the relevant fast5 files so we only need to open the model file once
	allNormalisedReads = []
	for f5File in fast5Files:

		#use 5mer model to calculate shift, scale, drift, and var to normalise events for the pore
		f = h5py.File(f5File,'r')
		path = '/Analyses/Basecall_1D_000/BaseCalled_template/Events'
		Events = f[path]
		A = np.zeros((2,2))
		b = np.zeros((2,1))
		for event in Events:
			 if float(event[7]) > 0.30: #if there's a high probability (>30%) that the 5mer model called by Metrichor was the correct one
				model_5mer = event[4]
				event_mean = float(event[0])
				model_mean = kmer2MeanStd[model_5mer][0]
				model_std = kmer2MeanStd[model_5mer][1]
				
				#update matrix A
				A[0,0] += 1/(model_std**2)
				A[1,0] += 1/(model_std**2)*model_mean
				A[1,1] += 1/(model_std**2)*model_mean**2

				#update vector b
				b[0] += 1/(model_std**2)*event_mean
				b[1] += 1/(model_std**2)*event_mean*model_mean

		#use symmetry of A
		A[0,1] = A[1,0]

		#solve Ax = b to find shift and scale
		x = np.linalg.solve(A,b)
		shift = x[0][0]
		scale = x[1][0]

		#go through the same events as before and normalise them to the pore model using scale and shift
		normalisedEvents = []
		for event in Events:
			if float(event[7]) > 0.30: #if there's a high probability (>30%) that the 5mer model called by Metrichor was the correct one
				event_mean = float(event[0])
				normalisedEvents.append( event_mean/scale - shift)

		allNormalisedReads.append(normalisedEvents)

		f.close()

	print 'Normalising for shift and scale... ' + 'finished ' + str(progress[0]) + ' of ' + str(progress[1])
	
	return (kmer, allNormalisedReads)


def serial_calculate_normalisedEvents(fast5Files, poreModelFile):
#	For fixed position training data - small enough to be done in serial.  Uses a 5mer model to calculate the shift and scale
#	pore-specific parameters for each individual read in a list of fast5 files
#	ARGUMENTS
#       ---------
#	- fast5Files: list of fast5 files whose events should be normalised
#	  type: list of strings
#	- poreModelFile: path to a pore model file.  This should be a 5mer model in the ONT format
#	  type: string
#	OUTPUTS
#       -------
#	- allNormalisedReads: a list, where each member is itself a list of events that have been normalised to the pore model
#	  type: list

	#open the 5mer model and make a map from the 5mer (string) to [mean,std] (list)
	kmer2MeanStd = import_poreModel(poreModelFile)

	#now iterate through all the relevant fast5 files so we only need to open the model file once
	allNormalisedReads = []
	for f5File in fast5Files:

		#use 5mer model to calculate shift, scale, drift, and var to normalise events for the pore
		f = h5py.File(f5File,'r')
		path = '/Analyses/Basecall_1D_000/BaseCalled_template/Events'
		Events = f[path]
		A = np.zeros((2,2))
		b = np.zeros((2,1))
		for event in Events:
			 if float(event[7]) > 0.30: #if there's a high probability (>30%) that the 5mer model called by Metrichor was the correct one
				model_5mer = event[4]
				event_mean = float(event[0])
				model_mean = kmer2MeanStd[model_5mer][0]
				model_std = kmer2MeanStd[model_5mer][1]
				
				#update matrix A
				A[0,0] += 1/(model_std**2)
				A[1,0] += 1/(model_std**2)*model_mean
				A[1,1] += 1/(model_std**2)*model_mean**2

				#update vector b
				b[0] += 1/(model_std**2)*event_mean
				b[1] += 1/(model_std**2)*event_mean*model_mean

		#use symmetry of A
		A[0,1] = A[1,0]

		#solve Ax = b to find shift and scale
		x = np.linalg.solve(A,b)
		shift = x[0][0]
		scale = x[1][0]

		#go through the same events as before and normalise them to the pore model using scale and shift
		normalisedEvents = []
		for event in Events:
			if float(event[7]) > 0.30: #if there's a high probability (>30%) that the 5mer model called by Metrichor was the correct one
				event_mean = float(event[0])
				normalisedEvents.append( event_mean/scale - shift)

		allNormalisedReads.append(normalisedEvents)

		f.close()
	
	return allNormalisedReads


def import_FixedPosTrainingData(bamFile, poreModelFile):
#	Used to import training data from reads that have an analogue in a fixed context.
#	Creates a map from kmer (string) to a list of lists, where each list is comprised of events from a read
#	First reads a BAM file to see which reads (readIDs, sequences) aligned to the references based on barcoding.  Then finds the fast5 files
#	that they came from, normalises the events to a pore model, and returns the list of normalised events.
#	ARGUMENTS
#       ---------
#	- bamFile: a BAM file from the alignment
#	  type: string
#	- poreModelFile: ONT model file for 5mers that can be used to normalised for shift and scale
#	  type: string
#	OUTPUTS
#       -------
#	- normalisedReads: a list of lists, where each element is a list of normalised events for a given read
#	  type: list

	#open up the BAM file that has been sorted by the reference that we're interested in
	f = pysam.AlignmentFile(bamFile,'r')

	#count the records in the bam file
	numRecords = f.count()
	print str(numRecords) + ' records in BAM file.'

	#iterate through the bam file, and for every record, add the path to the fast5 file to a list
	fast5files = []
	f = pysam.AlignmentFile(bamFile,'r')
	for record in f:

		fast5files.append(record.query_name)

	f.close()

	#hand this list of fast5 files to calculate_normalisedEvents which will normalise them for shift and scale
	normalisedReads = serial_calculate_normalisedEvents(fast5files, poreModelFile)

	return normalisedReads


def import_HairpinTrainingData(reference, bamFile, poreModelFile, redundant_A_Loc, readsThreshold):
#	Used to import training data from a hairpin primer of the form 5'-...NNNBNNN....NNNANNN...-3'.
#	Creates a map from kmer (string) to a list of lists, where each list is comprised of events from a read
#	First reads a BAM file to see which reads (readIDs, sequences) aligned to the references based on barcoding.  Then finds the fast5 files
#	that they came from, normalises the events to a pore model, and returns the list of normalised events.
#	ARGUMENTS
#       ---------
#	- reference: path to a fasta reference file
#	  type: string
#	- bamFile: a BAM file from the alignment
#	  type: string
#	- poreModelFile: ONT model file for 5mers that can be used to normalised for shift and scale
#	  type: string
#	- redundant_A_Loc: location of the redundant A that is the reverse complement of BrdU (starting from 0)
#	  type: int
#	- readsThreshold: disregard a NNNANNN 7mer that only has a number of high quality reads below this threshold
#	  type: int
#	OUTPUTS
#       -------
#	- kmer2normalisedReads: a dictionary that takes a kmer string as a key and outputs a list of lists, where each list gives the normalised events from an individual read
#	  type: dictionary

	#open bam file	
	f = pysam.AlignmentFile(bamFile,'r')

	#count and print number of entries in bam file to stdout
	numRecords = f.count()
	print str(numRecords) + ' records in BAM file.'

	#build up the map that takes each indiviudal 7mer to a list of fast5 files that produced the reads
	kmer2Files = {}
	f = pysam.AlignmentFile(bamFile,'r')
	for record in f:

		sequence = record.query_sequence
		readID = record.query_name

		#grab the part of the sequence that's flanked by start and end.  there may be more than one candidate.
		candidates = []
		start = reference[redundant_A_Loc-7:redundant_A_Loc-3] #four bases on the 5' end of the NNNANNN domain
		end = reference[redundant_A_Loc+4:redundant_A_Loc+8] #four bases on the 3' end of the NNNANNN domain
		start_indices = [s.start() for s in re.finditer('(?=' + start + ')', sequence)] #find all (possibly overlapping) indices of start using regular expressions
		end_indices = [s.start() for s in re.finditer('(?=' + end + ')', sequence)] #same for end
		for si in start_indices:
			si = si + len(start)
			for ei in end_indices:
				if ei > si:
					candidate = sequence[si:ei] #grab the subsequence between the start and end index
					if len(candidate) == 7 and candidate[3] == 'A': #consider it a candidate if it's a 7mer and has an A in the middle
						candidates.append(candidate)

		#only add the read to the map if we're sure that we've found exactly one correct redundant 7mer, and its reverse complement is in the sequence
		if len(candidates) == 1:
			idx_brdu = sequence.find(reverseComplement(candidates[0]))
			idx_a = sequence.find(candidates[0])
			if idx_brdu != -1 and idx_brdu < idx_a:
				if candidates[0] in kmer2Files:
					kmer2Files[candidates[0]] += [readID]
				else:
					kmer2Files[candidates[0]] = [readID]

	f.close()

	#if a kmer has a number of associated reads that is below the minimum number of reads we need to train on, remove that kmer from the dictionary
	filteredKmer2Files = {}
	for key in kmer2Files:
		if len(kmer2Files[key]) >= readsThreshold:
			filteredKmer2Files[key] = kmer2Files[key]
	del kmer2Files

	#do the parallel processing, where a kmer (and associated reads) is given to each core.  use the maximum number of cores available
	normalisedReadsTuples = Parallel(n_jobs = multiprocessing.cpu_count())(delayed(parallel_calculate_normalisedEvents)(key, filteredKmer2Files[key], poreModelFile, (i,len(filteredKmer2Files))) for i, key in enumerate(filteredKmer2Files))

	#reshape the list of tuples from parallel processing into a dictionary
	kmer2normalisedReads = {}
	for entry in normalisedReadsTuples:
		kmer2normalisedReads[entry[0]] = entry[1]

	return kmer2normalisedReads


def alignAndSort(readsDirectory, pathToReference, threads):
#	takes reads from a run, aligns them to a reference, and separates the resulting bam file by each reference
#	ARGUMENTS
#       ---------
#	- readsDirectory: full path to the directory that contains the fast5 files for the run 
#	  type: string
#	- pathToReference: full path to the reference file that has the reference (or references, if they're barcoded) for all sequences present in the run
#	  type: string
#	- threads: number of threads on which to run BWA-MEM 
#	  type: int 
#	OUTPUTS
#       -------
#	- in the present working directory, a reads.fasta file is created that has sequences of all the reads in the run, and bam files are created for each reference
	
	#take the directory with the fast5 reads in it and export them to a fasta file in the current working directory
	import_2Dfasta(readsDirectory, os.getcwd()+'/reads.fasta')

	#index the reference
	os.system('bwa index ' + pathToReference)

	#align the reads.fasta file created above to the reference with bwa-mem, then sort the bam file
	os.system('bwa mem -t '+str(threads)+' -k 1 -x ont2d '+pathToReference+' reads.fasta | samtools view -Sb - | samtools sort -o alignments.sorted.bam -') 
	os.system('samtools index alignments.sorted.bam')

	sam_file = pysam.Samfile('alignments.sorted.bam')
	out_files = list()

	#take alignments.sorted.bam and separate the records into separate bam files - one for each reference
	for x in sam_file.references:
		print x
		out_files.append(pysam.Samfile(x + ".bam", "wb", template=sam_file))

	for record in sam_file:
		ref_length = sam_file.lengths[record.reference_id]

		if record.aend is None or record.query_alignment_length is None:
			continue

		ref_cover = float(record.aend - record.pos) / ref_length
		query_cover = float(record.query_alignment_length) / record.query_length

		#quality control for the reads that make it to the BAM file: only take >80% coverage with no reverse complement
		if ref_cover > 0.8 and query_cover > 0.8 and record.is_reverse == False:
			out_files[record.reference_id].write(record)

	#index the newly made bam files
	for bamfile in sam_file.references:
		os.system('samtools index '+bamfile + '.bam')
	

	

