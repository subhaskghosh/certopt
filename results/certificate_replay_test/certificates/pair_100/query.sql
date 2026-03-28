SELECT
  person.firstname,
  person.lastname,
  address.city,
  address.state
FROM PERSON
LEFT JOIN ADDRESS
  ON address.personid = person.personid AND 1 = 1