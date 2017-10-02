//----------------------------------------------------------
// Copyright 2017 University of Oxford
// Written by Michael A. Boemo (michael.boemo@path.ox.ac.uk)
// This software is licensed under GPL-3.0.  You should have
// received a copy of the license with this software.  If
// not, please Email the author.
//----------------------------------------------------------

#ifndef ERROR_HANDLING_H
#define ERROR_HANDLING_H

#include <exception>
#include <string.h>

struct IOerror : public std::exception {
	std::string badFilename;	
	IOerror( std::string s ){

		badFilename = s;
	}
	const char* what () const throw () {
		const char* message = "Could not open file: ";
		const char* specifier = badFilename.c_str();
		char* result;
		result = static_cast<char*>(calloc(strlen(message)+strlen(specifier)+1, sizeof(char)));
		strcpy( result, message);
		strcat( result, specifier );

		return result;
	}	
};


struct InvalidOption : public std::exception {
	std::string badOption;	
	InvalidOption( std::string s ){

		badOption = s;
	}
	const char* what () const throw () {
		const char* message = "Invalid option passed: ";
		const char* specifier = badOption.c_str();
		char* result;
		result = static_cast<char*>(calloc(strlen(message)+strlen(specifier)+1, sizeof(char)));
		strcpy( result, message);
		strcat( result, specifier );

		return result;
	}	
};

#endif

