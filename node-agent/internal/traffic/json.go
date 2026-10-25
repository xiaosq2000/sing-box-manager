package traffic

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"unicode/utf16"
	"unicode/utf8"
)

const maxJSONDepth = 1000

// parseDocument decodes one bounded JSON document into standard-library
// values. Objects reject duplicate decoded keys, so escaped spellings that
// decode to the same key collide. String escapes are checked before the
// standard decoder so lone surrogate escapes cannot be silently replaced by
// U+FFFD.
func parseDocument(raw []byte) (any, error) {
	if len(raw) > MaxRawBodyBytes {
		return nil, contractError(ErrRequestTooLarge)
	}
	if !utf8.Valid(raw) || !validStringEscapes(raw) {
		return nil, contractError(ErrInvalidRequest)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	value, err := decodeValue(decoder, 0)
	if err != nil {
		return nil, contractError(ErrInvalidRequest)
	}
	token, err := decoder.Token()
	if err == nil {
		_ = token
		return nil, contractError(ErrInvalidRequest)
	}
	if !errors.Is(err, io.EOF) {
		return nil, contractError(ErrInvalidRequest)
	}
	return value, nil
}

func decodeValue(decoder *json.Decoder, depth int) (any, error) {
	if depth > maxJSONDepth {
		return nil, errors.New("json nesting too deep")
	}
	token, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	delimiter, isDelimiter := token.(json.Delim)
	if !isDelimiter {
		switch token.(type) {
		case string, json.Number, bool, nil:
			return token, nil
		default:
			return nil, errors.New("unexpected json token")
		}
	}
	switch delimiter {
	case '{':
		object := make(map[string]any)
		for decoder.More() {
			keyToken, err := decoder.Token()
			if err != nil {
				return nil, err
			}
			key, ok := keyToken.(string)
			if !ok {
				return nil, errors.New("object key is not a string")
			}
			if _, duplicate := object[key]; duplicate {
				return nil, errors.New("duplicate object key")
			}
			value, err := decodeValue(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			object[key] = value
		}
		end, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		if end != json.Delim('}') {
			return nil, errors.New("object is not closed")
		}
		return object, nil
	case '[':
		array := make([]any, 0)
		for decoder.More() {
			value, err := decodeValue(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			array = append(array, value)
		}
		end, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		if end != json.Delim(']') {
			return nil, errors.New("array is not closed")
		}
		return array, nil
	default:
		return nil, errors.New("unexpected json delimiter")
	}
}

// validStringEscapes rejects unpaired UTF-16 surrogate escapes in every JSON
// string. Valid pairs remain valid, and a literal U+FFFD is valid UTF-8 input.
func validStringEscapes(raw []byte) bool {
	for index := 0; index < len(raw); index++ {
		if raw[index] != '"' {
			continue
		}
		index++
		for index < len(raw) {
			character := raw[index]
			if character == '"' {
				break
			}
			if character != '\\' {
				index++
				continue
			}
			index++
			if index >= len(raw) {
				return false
			}
			if raw[index] != 'u' {
				index++
				continue
			}
			codePoint, next, ok := readHex4(raw, index+1)
			if !ok {
				return false
			}
			index = next
			if !utf16.IsSurrogate(codePoint) {
				continue
			}
			if codePoint < 0xD800 || codePoint > 0xDBFF {
				return false
			}
			if index+1 >= len(raw) || raw[index] != '\\' || raw[index+1] != 'u' {
				return false
			}
			low, next, ok := readHex4(raw, index+2)
			if !ok || low < 0xDC00 || low > 0xDFFF {
				return false
			}
			index = next
		}
	}
	return true
}

func readHex4(raw []byte, start int) (rune, int, bool) {
	if start+4 > len(raw) {
		return 0, 0, false
	}
	var value rune
	for index := start; index < start+4; index++ {
		character := raw[index]
		var digit byte
		switch {
		case character >= '0' && character <= '9':
			digit = character - '0'
		case character >= 'a' && character <= 'f':
			digit = character - 'a' + 10
		case character >= 'A' && character <= 'F':
			digit = character - 'A' + 10
		default:
			return 0, 0, false
		}
		value = value<<4 | rune(digit)
	}
	return value, start + 4, true
}
