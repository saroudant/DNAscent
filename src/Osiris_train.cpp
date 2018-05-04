//----------------------------------------------------------
// Copyright 2017 University of Oxford
// Written by Michael A. Boemo (michael.boemo@path.ox.ac.uk)
// This software is licensed under GPL-3.0.  You should have
// received a copy of the license with this software.  If
// not, please Email the author.
//----------------------------------------------------------

#include <exception>
#include <math.h>
#include <iostream>
#include <fstream>
#include "common.h"
#include "data_IO.h"
#include "error_handling.h"
#include "event_handling.h"
#include "../Penthus/src/error_handling.h"
#include "../Penthus/src/hmm.h"
#include "../Penthus/src/states.h"
#include "../Penthus/src/unsupervised_learning.h"
#include "poreModels.h"
#include "Osiris_train.h"
#include "poreSpecificParameters.h"

static const char *help=
"train: Osiris executable that determines the mean and standard deviation of a base analogue's current.\n"
"To run Osiris train, do:\n"
"  ./Osiris train [arguments]\n"
"Example:\n"
"  ./Osiris train -d /path/to/data.foh -b 150 650 -o output.txt -t 20\n"
"Required arguments are:\n"
"  -d,--trainingData         path to training data in the .foh format (made with prepTrainingData.py),\n"
"  -b,--bounds               indices of where the de Bruijn sequence starts and ends in the reference,\n"
"  -o,--output               path to the output pore model file that Osiris will train.\n"
"Optional arguments are:\n"
"  -t,--threads              number of threads (default is 1 thread).\n";

struct Arguments {

	std::string trainingDataFilename;
	std::string trainingOutputFilename;
	bool logFile;
	std::string logFilename;
	int threads;
	int boundLower;
	int boundUpper;
};

Arguments parseTrainingArguments( int argc, char** argv ){

	if( argc < 2 ) throw InsufficientArguments();

	if ( std::string( argv[ 1 ] ) == "-h" or std::string( argv[ 1 ] ) == "--help" ){

		std::cout << help << std::endl;
		exit(EXIT_SUCCESS);
	}
	//else if( argc < 4 ) throw InsufficientArguments();

	Arguments trainArgs;

	/*defaults - we'll override these if the option was specified by the user */
	trainArgs.threads = 1;

	/*parse the command line arguments */
	for ( int i = 1; i < argc; ){

		std::string flag( argv[ i ] );

		if ( flag == "-d" or flag == "--trainingData" ){

			std::string strArg( argv[ i + 1 ] );
			trainArgs.trainingDataFilename = strArg;
			i+=2;
		}
		else if ( flag == "-o" or flag == "--output" ){

			std::string strArg( argv[ i + 1 ] );
			trainArgs.trainingOutputFilename = strArg;
			i+=2;
		}
		else if ( flag == "-b" or flag == "--bounds" ){

			std::string strLower( argv[ i + 1 ] );
			trainArgs.boundLower = std::stoi( strLower.c_str() );
			std::string strUpper( argv[ i + 2 ] );
			trainArgs.boundUpper = std::stoi( strUpper.c_str() );
			i+=3;
		}
		else if ( flag == "-t" or flag == "--threads" ){

			std::string strArg( argv[ i + 1 ] );
			trainArgs.threads = std::stoi( strArg.c_str() );
			i+=2;
		}
		else throw InvalidOption( flag );
	}
	return trainArgs;
}


int train_main( int argc, char** argv ){

	Arguments trainArgs = parseTrainingArguments( argc, argv );

	/*get a filestream to the foh file - we'll load training data dynamically */
	std::ifstream fohFile( trainArgs.trainingDataFilename );
	if ( not fohFile.is_open() ) throw IOerror( trainArgs.trainingDataFilename );

	/*read the foh header - total count and reference */
	std::string line, reference;
	std::getline( fohFile, reference );
	std::getline( fohFile, line );
	int trainingTotal = atoi(line.c_str());

	/*initialise progress */
	progressBar pb_align( trainingTotal );
	int prog = 0, offloadCount = 0, failed = 0;

	/*open output file */
	std::ofstream outFile( trainArgs.trainingOutputFilename );
	if ( not outFile.is_open() ) throw IOerror( trainArgs.trainingOutputFilename );

	/*buffers */
	std::map< int, std::vector< double > > eventPileup;
	std::vector< read > buffer;

	//#if false
	/*open work file */
	std::ofstream workFile( "workingData.osiris" );
	if ( not workFile.is_open() ) throw IOerror( "workingData.osiris" );

	/*align the training data */
	std::cout << "Aligning events..." << std::endl;
	while ( std::getline( fohFile, line) ){
		
		/*get data for a read from foh */
		read currentRead;

		/*the basecall line */
		currentRead.basecalls = line;

		/*the reference bounds line */
		std::getline( fohFile, line );
		(currentRead.bounds_reference).first = atoi( (line.substr( 0, line.find(' ') )).c_str() );
		(currentRead.bounds_reference).second = atoi( (line.substr( line.find(' ') + 1, line.size() - line.find(' ') )).c_str() );

		/*the query bounds line */
		std::getline( fohFile, line );
		(currentRead.bounds_query).first = atoi( (line.substr( 0, line.find(' ') )).c_str() );
		(currentRead.bounds_query).second = atoi( (line.substr( line.find(' ') + 1, line.size() - line.find(' ') )).c_str() );

		/*the raw signal line */
		std::getline( fohFile, line );
		std::vector< double > rawSignals;
		std::istringstream ss( line );
		std::string event;
		while ( std::getline( ss, event, ' ' ) ){

			rawSignals.push_back( atof( event.c_str() ) );
		}
		currentRead.raw = rawSignals;

		/*push it to the buffer or run Viterbi if the buffer is full */
		buffer.push_back( currentRead );
		if ( (buffer.size() < trainArgs.threads)  ) continue;

		/*Viterbi event alignment */
		#pragma omp parallel for default(none) shared(reference,failed,pb_align,eventPileup,buffer,trainArgs,trainingTotal,prog,FiveMer_model,internalSS2M1, internalSS2M2, internalI2I, internalI2SS, internalM12M1, internalM12SE, internalM22M2, internalM22SE, internalSE2I, externalD2D, externalD2SS, externalI2SS, externalSE2D, externalSE2SS) num_threads(trainArgs.threads)
		for ( auto read = buffer.begin(); read < buffer.end(); read++ ){

			/*normalise for shift and scale */
			eventDataForRead eventData = normaliseEvents( *read );

			/*disregard this event if the quality score is too low */
			if ( fabs(eventData.qualityScore) > 1.0 ){

				failed++;
				prog++;
				continue;
			}

			/*get the subsequence of the reference this read mapped to and build an HMM from it */
			std::string refSeqMapped = reference.substr(((*read).bounds_reference).first, ((*read).bounds_reference).second - ((*read).bounds_reference).first);
			HiddenMarkovModel hmm = HiddenMarkovModel( 3*refSeqMapped.length(), 3*refSeqMapped.length() + 2 );

			/*STATES - vector (of vectors) to hold the states at each position on the reference - fill with dummy values */
			std::vector< std::vector< State > > states( 6, std::vector< State >( refSeqMapped.length() - 5, State( NULL, "", "", "", 1.0 ) ) );

			/*DISTRIBUTIONS - vector to hold normal distributions, a single uniform and silent distribution to use for everything else */
			std::vector< NormalDistribution > nd, ndWide;
			nd.reserve( refSeqMapped.length() - 5 );
			ndWide.reserve( refSeqMapped.length() - 5 );				
			SilentDistribution sd( 0.0, 0.0 );
			UniformDistribution ud( 50.0, 150.0 );

			std::string loc, fiveMer;

			/*create make normal distributions for each reference position using the ONT 5mer model */			
			for ( unsigned int i = 0; i < refSeqMapped.length() - 5; i++ ){

				fiveMer = refSeqMapped.substr( i, 5 );
				nd.push_back( NormalDistribution( FiveMer_model[fiveMer].first, FiveMer_model[fiveMer].second ) );
				ndWide.push_back( NormalDistribution( FiveMer_model[fiveMer].first, 2*(FiveMer_model[fiveMer].second) ) );				
			}

			/*add states to the model, handle internal module transitions */
			for ( unsigned int i = 0; i < refSeqMapped.length() - 5; i++ ){

				loc = std::to_string( i + ((*read).bounds_reference).first );
				fiveMer = refSeqMapped.substr( i, 5 );

				states[ 0 ][ i ] = State( &sd,		loc + "_D", 	fiveMer,	"", 		1.0 );		
				states[ 1 ][ i ] = State( &ud,		loc + "_I", 	fiveMer,	"", 		1.0 );
				states[ 2 ][ i ] = State( &nd[i], 	loc + "_M1", 	fiveMer,	loc + "_match", 1.0 );
				states[ 3 ][ i ] = State( &ndWide[i], 	loc + "_M2", 	fiveMer,	loc + "_match", 1.0 );

				/*add state to the model */
				for ( unsigned int j = 0; j < 4; j++ ){

					states[ j ][ i ].meta = fiveMer;
					hmm.add_state( states[ j ][ i ] );
				}

				/*transitions between states, internal to a single base */
	
				/*from D */
				//hmm.add_transition( states[0][i], states[1][i], internalD2I );

				/*from I */
				hmm.add_transition( states[1][i], states[1][i], internalI2I );
				hmm.add_transition( states[1][i], states[2][i], internalI2SS*internalSS2M1 );
				hmm.add_transition( states[1][i], states[3][i], internalI2SS*internalSS2M2 );

				/*from M1 */
				hmm.add_transition( states[2][i], states[2][i], internalM12M1 );
				//hmm.add_transition( states[2][i], states[3][i], internalM12M2 );
				hmm.add_transition( states[2][i], states[1][i], internalM12SE*internalSE2I );

				/*from M2 */
				hmm.add_transition( states[3][i], states[3][i], internalM22M2 );
				//hmm.add_transition( states[3][i], states[2][i], internalM22M1 );
				hmm.add_transition( states[3][i], states[1][i], internalM22SE*internalSE2I );
			}

			/*add transitions between modules (external transitions) */
			for ( unsigned int i = 0; i < refSeqMapped.length() - 6; i++ ){

				hmm.add_transition( states[0][i], states[0][i + 1], externalD2D );
				hmm.add_transition( states[0][i], states[2][i + 1], externalD2SS*internalSS2M1 );
				hmm.add_transition( states[0][i], states[3][i + 1], externalD2SS*internalSS2M2 );

				hmm.add_transition( states[1][i], states[2][i + 1], externalI2SS*internalSS2M1 );
				hmm.add_transition( states[1][i], states[3][i + 1], externalI2SS*internalSS2M2 );

				hmm.add_transition( states[2][i], states[0][i + 1], internalM12SE*externalSE2D );
				hmm.add_transition( states[2][i], states[2][i + 1], internalM12SE*externalSE2SS*internalSS2M1 );
				hmm.add_transition( states[2][i], states[3][i + 1], internalM12SE*externalSE2SS*internalSS2M2 );

				hmm.add_transition( states[3][i], states[0][i + 1], internalM22SE*externalSE2D );
				hmm.add_transition( states[3][i], states[2][i + 1], internalM22SE*externalSE2SS*internalSS2M1 );
				hmm.add_transition( states[3][i], states[3][i + 1], internalM22SE*externalSE2SS*internalSS2M2 );
			}

			/*handle start states */
			hmm.add_transition( hmm.start, states[1][0], 0.5 );
			hmm.add_transition( hmm.start, states[2][0], 0.5*internalSS2M1 );
			hmm.add_transition( hmm.start, states[3][0], 0.5*internalSS2M2 );

			/*handle end states */
			hmm.add_transition( states[0][refSeqMapped.length() - 6], hmm.end, externalD2D + externalD2SS );
			hmm.add_transition( states[1][refSeqMapped.length() - 6], hmm.end, externalI2SS );
			hmm.add_transition( states[2][refSeqMapped.length() - 6], hmm.end, internalM12SE*externalSE2SS + internalM12SE*externalSE2D );
			hmm.add_transition( states[3][refSeqMapped.length() - 6], hmm.end, internalM22SE*externalSE2SS + internalM22SE*externalSE2D );

			hmm.finalise();

			/*do the event alignment with the Penthus Viterbi algorithm */
			auto viterbiData = hmm.viterbi( eventData.normalisedEvents ); 
			double viterbiScore = viterbiData.first;
			if  ( std::isnan( viterbiScore ) ){

				failed++;
				prog++;
				continue;
			}	
			std::vector< std::string > statePath = viterbiData.second;

			/*filter state path to only emitting states */
			std::vector< std::string > emittingStatePath;
			for ( auto s = statePath.begin(); s < statePath.end(); s++ ){

				if ( (*s).substr((*s).find('_') + 1,1) == "M" or (*s).substr((*s).find('_') + 1,1) == "I" ){
					emittingStatePath.push_back( *s );
				}
			}

			/*add the events to the eventPileup hash table - these events are keyed by their position in the reference */
			#pragma omp critical
			{
				for ( unsigned int i = 0; i < (eventData.normalisedEvents).size(); i++ ){

					std::vector< std::string > findIndex = split( emittingStatePath[i], '_' );
					unsigned int posOnReference = atoi(findIndex[0].c_str());

					if ( posOnReference >= trainArgs.boundLower and posOnReference < trainArgs.boundUpper and findIndex[1].substr(0,1) == "M" ){
						//std::cout << (eventData.normalisedEvents)[i] << '\t' << posOnReference << '\t' << emittingStatePath[i] << '\t' << reference.substr(posOnReference,5) << '\t' << FiveMer_model[reference.substr(posOnReference,5)].first << '\t' << FiveMer_model[reference.substr(posOnReference,5)].second << std::endl;
						eventPileup[posOnReference].push_back( (eventData.normalisedEvents)[i] );
					}
				}
			pb_align.displayProgress( prog, failed );
			prog++;
			}
		}
		offloadCount++;
		buffer.clear();

		/*after running through the buffer a few times, offload event data to file so we don't flood memory */
		if ( offloadCount == 5 ){

			for ( auto iter = eventPileup.cbegin(); iter != eventPileup.cend(); ++iter ){

				workFile << iter -> first;
				for ( auto e = (iter -> second).begin(); e < (iter -> second).end(); e++ ){

					workFile << ' ' << *e;
				}
				workFile << std::endl;
			}
			offloadCount = 0;
			eventPileup.clear();
		}
	}

	/*wrap up */
	fohFile.close();
	workFile.close();
	eventPileup.clear();
	pb_align.displayProgress( trainingTotal, failed );
	failed = prog = 0;
	std::cout << std::endl << "Done." << std::endl;

	//#endif

	/*get events from the work file */
	std::cout << "Fitting Gaussian mixture model..." << std::endl;

	std::ifstream eventFile( "workingData.osiris" );
	if ( not eventFile.is_open() ) throw IOerror( "workingData.osiris" );

	std::vector< std::vector< double > > importedEvents( trainArgs.boundUpper - trainArgs.boundLower );

	while ( std::getline( eventFile, line) ){

		std::vector< double > rawSignals;
		std::istringstream ss( line );
		std::string event;
		std::getline( ss, event, ' ' );
		int position = atoi(event.c_str());

		while ( std::getline( ss, event, ' ' ) ){

			rawSignals.push_back( atof( event.c_str() ) );
		}
		importedEvents[position-trainArgs.boundLower].insert( importedEvents[position-trainArgs.boundLower].end(), rawSignals.begin(), rawSignals.end() );
	}
	eventFile.close();

	/*fit a mixture model to the events that aligned to each position in the reference */
	outFile << "5mer" << '\t' << "ONT_mean" << '\t' << "ONT_stdv" << '\t' << "pi_1" << '\t' << "mean_1" << '\t' << "stdv_1" << '\t' << "pi_2" << '\t' << "mean_2" << '\t' << "stdv_2" << std::endl;
	progressBar pb_fit( importedEvents.size() );

	#pragma omp parallel for default(none) shared(pb_fit, FiveMer_model, prog, failed, outFile, importedEvents, trainArgs, reference) num_threads(trainArgs.threads)
	for ( int i = 0; i < importedEvents.size(); i++ ){

		double mu1, stdv1, mu2, stdv2;

		/*get the ONT distribution for the mixture */
		std::string fiveMer = reference.substr( i + trainArgs.boundLower, 5 );
		mu1 = FiveMer_model[fiveMer].first;
		stdv1 = FiveMer_model[fiveMer].second;

		/*make a second distribution that's similar to the ONT distribution */
		mu2 = FiveMer_model[fiveMer].first;
		stdv2 = 2*FiveMer_model[fiveMer].second;

		/*fit the model */
		std::vector< double > fitParameters;
		try{
			fitParameters = gaussianMixtureEM_PRIOR( mu1, stdv1, mu2, stdv2, importedEvents[i], 0.0001 );
		}
		catch ( NegativeLog &nl ){

			failed++;
			prog++;
			continue;
		}
		#pragma omp critical
		{
			outFile << fiveMer << '\t' << i + trainArgs.boundLower << '\t' << mu1 << '\t' << stdv1 << '\t' << fitParameters[0] << '\t' << fitParameters[1] << '\t' << fitParameters[2] << '\t' << fitParameters[3] << '\t' << fitParameters[4] << '\t' << fitParameters[5] << std::endl; 

			pb_fit.displayProgress( prog, failed );
			prog++;
		}
	}
	outFile.close();
	std::cout << std::endl << "Done." << std::endl;

	return 0;
}
