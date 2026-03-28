SELECT
  person.firstname,
  person.lastname,
  city,
  state
FROM PERSON
LEFT JOIN ADDRESS
  ON address.personid = person.personid